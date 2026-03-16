# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright Abdullah-Al Amin 2022
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Modified:
#
# - Martin Ahindura 2023
import copy
from datetime import datetime
from typing import Optional, Tuple
from uuid import UUID

from fastapi import Depends, FastAPI, File, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import FileResponse
from pydantic import ValidationError
from redis import Redis
from sqlalchemy import Engine
from typing_extensions import Annotated

import settings
from app.services import booking, scheduler
from app.services.booking.models import (
    Booking,
    BookingsConfig,
    MSSTokenClaims,
    NewBookingInfo,
    NewUserInfo,
    User,
    UserProfile,
)
from app.utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
    InvalidJobIdInUploadedFileError,
    InvalidRequestError,
    JobAlreadyCancelled,
    JobAlreadyCompleteError,
    MaxBookingsError,
    NotAllowedError,
    NotAuthenticatedError,
    UnauthorizedError,
)

from ..libs import device_parameters as props_lib
from ..libs.queues.dtos import Job, JobStatus, QueueContext
from ..services.booking import get_user
from ..services.scheduler.queues import QueuePool
from ..utils.api import (
    CancellationDetails,
    GeneralMessage,
    PaginatedListResponse,
    RequestLog,
    TokenResponse,
    encrypt_mss_jwt_token,
    get_request_logs_store,
    to_http_error,
)
from ..utils.redis_store import ItemNotFoundError
from ..utils.sql_db import convert_http_sort_to_db_sort
from ..utils.strings import uuid_str
from .dependencies import (
    MSSAuthDetails,
    get_backend_name,
    get_bearer_token,
    get_cached_queue_context,
    get_cached_redis_connection,
    get_db_engine,
    get_mss_token_claims_dep,
    get_queue_pool,
    get_unverified_mss_is_admin,
    get_verified_mss_admin_user_id,
    get_verified_mss_details,
    get_verified_mss_user_id,
    lifespan,
    validate_job_file,
)

# application
app = FastAPI(
    title="Backend Control Computer",
    description="Interfaces Quantum processor via REST API",
    version="2026.03.0",
    lifespan=lifespan,
)

# exception handlers
app.add_exception_handler(NotAllowedError, to_http_error(405))
app.add_exception_handler(NotAuthenticatedError, to_http_error(401))
app.add_exception_handler(UnauthorizedError, to_http_error(403))
app.add_exception_handler(ItemNotFoundError, to_http_error(404))
app.add_exception_handler(JobAlreadyCompleteError, to_http_error(406))
app.add_exception_handler(ConflictError, to_http_error(409))
app.add_exception_handler(MaxBookingsError, to_http_error(400))
app.add_exception_handler(InvalidRequestError, to_http_error(400))
app.add_exception_handler(BookingAlreadyCompleteError, to_http_error(400))
app.add_exception_handler(BookingAlreadyActiveError, to_http_error(400))
app.add_exception_handler(ValidationError, to_http_error(422))
app.add_exception_handler(ValueError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(IndexError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(TypeError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(RuntimeError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(InvalidJobIdInUploadedFileError, to_http_error(400))
app.add_exception_handler(ItemNotFoundError, to_http_error(404))
app.add_exception_handler(JobAlreadyCancelled, to_http_error(406))

# setup CORS if CORS_ORIGINS are set
if len(settings.CORS_ORIGINS) > 0:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def add_request_id_header(request: Request, call_next):
    """Adds an `x-request-id` header

    It will get it from `x-mss-request-id` if that is present or generate a new one

    Args:
        request: the current FastAPI request object
        call_next: the callback that calls the next middleware or route handler
    """
    request_id = request.headers.get("x-mss-request-id")
    if request_id is None:
        request_id = uuid_str()

    request.state.request_id = request_id

    response = await call_next(request)

    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Adds an `x-request-id` header and logs requests received

    It will get it from `x-mss-request-id` if that is present or generate a new one

    Args:
        request: the current FastAPI request object
        call_next: the callback that calls the next middleware or route handler
    """
    request_id = request.headers.get("x-mss-request-id")
    if request_id is None:
        request_id = uuid_str()

    request.state.request_id = request_id

    response = await call_next(request)

    requests_store = get_request_logs_store()
    if not requests_store.exists(request_id):
        requests_store.insert(RequestLog.from_request(request))

    response.headers["X-Request-ID"] = request_id
    return response


# routing
@app.get("/", dependencies=[Depends(get_verified_mss_user_id)])
async def root():
    return {"message": "Welcome to BCC machine"}


@app.get(
    "/logfiles/{logfile_id}",
    dependencies=[Depends(get_mss_token_claims_dep(job_id_field="logfile_id"))],
)
async def download_logfile(logfile_id: UUID):
    """Downloads the job logfile

    Args:
        logfile_id: the id of the logfile usually the job id

    Raises:
        ItemNotFoundError: logfile {logfile_id} not found
    """
    file = (settings.LOG_FILE_POOL / str(logfile_id)).with_suffix(".hdf5")
    if file.exists():
        return FileResponse(file)
    raise ItemNotFoundError(f"logfile {logfile_id} not found")


@app.get("/static-properties", dependencies=[Depends(get_verified_mss_user_id)])
async def get_static_properties(
    redis: Redis = Depends(get_cached_redis_connection),
    backend_name: str = Depends(get_backend_name),
):
    """Retrieves the device properties that are not changing"""
    return props_lib.get_device_info(redis, backend_name)


@app.get("/dynamic-properties", dependencies=[Depends(get_verified_mss_user_id)])
async def get_dynamic_properties(
    redis: Redis = Depends(get_cached_redis_connection),
    backend_name: str = Depends(get_backend_name),
):
    """Retrieves the device properties that are changing with time i.e. calibration data"""
    return props_lib.get_device_calibration_info(redis, backend_name)


@app.get("/me")
async def view_profile(
    db_engine: Engine = Depends(get_db_engine),
    user_id: str = Depends(get_verified_mss_user_id),
) -> UserProfile:
    """Views the profile of the current user

    Args:
        db_engine: the SQL database to query
        user_id: the user_id as submitted by MSS

    Raises:
        ItemNotFoundError (404): user not found

    Returns:
        the user profile of the current user
    """
    return booking.get_user_profile(db_engine, user_id)


@app.delete("/me")
async def delete_profile(
    user_id: str = Depends(get_verified_mss_user_id),
    context: QueueContext = Depends(get_cached_queue_context),
    queue_pool: QueuePool = Depends(get_queue_pool),
) -> GeneralMessage:
    """Deletes the profile of the current user

    Args:
        user_id: the user_id as submitted by MSS
        context: the queue context of the queues for all jobs
        queue_pool: the collection of queues where the jobs run

    Raises:
        ItemNotFoundError (404): user not found

    Returns:
        A general message object with status
    """
    scheduler.delete_user_profile(context, queues=queue_pool, user_id=user_id)
    return GeneralMessage(status="success", detail="Profile deleted")


@app.post("/token")
async def get_mss_token(
    body: MSSTokenClaims,
    mss_user_id: str = Depends(get_verified_mss_user_id),
    db_engine: Engine = Depends(get_db_engine),
) -> TokenResponse:
    """Get a token specific to the associated MSS instance for the provided token claims.

    This is a special route as it can only be used through the associated MSS instance.
    On top of that MSS being whitelisted, the public key of that MSS will be used to encrypt
    the access token that will be sent over to the user.

    Take note that the JWT must also contain the `user_id` as well as the `job_id`
    as these will be used to ensure a JWT is not used for another job.

    Take note also that if the given user does not exist, the user is created with
    a random email and password.

    Args:
        db_engine: the SQL database to query
        body: the http request body sent
        mss_user_id: the user_id as specified by MSS

    Returns:
        the user token response
    """
    user_id = body.user_id
    job_id = body.job_id

    if user_id != mss_user_id:
        # just some house-keeping to ensure the two user ids are the same
        raise UnauthorizedError("Forbidden")

    user = get_user(db_engine, User.id == user_id)
    if user is None:
        # create a random user if the user does not exist
        user = booking.create_random_user(db_engine, user_id)

    # create encrypted token
    token = booking.create_mss_jwt_token(user, job_id=job_id)
    encrypted_token = encrypt_mss_jwt_token(token)
    return {"access_token": encrypted_token, "token_type": "bearer"}


@app.post("/users", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def create_user(
    data: NewUserInfo, db_engine: Engine = Depends(get_db_engine)
) -> UserProfile:
    """Creates a user given the name and email

    Only MSS admin users can create users here

    Args:
        data: the information about the new user
        db_engine: the SQL database to query

    Returns:
        the created user
    """
    user = booking.create_user(db_engine, data=data)
    return UserProfile.from_user(user)


@app.delete("/users/{user_id}", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def remove_user(
    user_id: str,
    context: QueueContext = Depends(get_cached_queue_context),
    queue_pool: QueuePool = Depends(get_queue_pool),
) -> GeneralMessage:
    """Deletes the user of the given user_id

    Only admins are allowed to remove users

    Args:
        user_id: the unique identifier of the user
        context: the context of the queues for all jobs
        queue_pool: the collection of queues to run the jobs on

    Raises:
        ItemNotFoundError: user not found

    Returns:
        A general message object with status
    """
    scheduler.delete_user_profile(context, queues=queue_pool, user_id=user_id)
    return GeneralMessage(status="success", detail="User deleted")


@app.get("/users", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def view_users(
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
    db_engine: Engine = Depends(get_db_engine),
) -> PaginatedListResponse[UserProfile]:
    """Views all users

    Only MSS admin users can view this

    Args:
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.
        db_engine: the SQL database to query

    Returns:
        the paginated list of the available users
    """
    data = booking.get_many_user_profiles(db_engine, skip=skip, limit=limit)
    return PaginatedListResponse(skip=skip, limit=limit, data=data)


@app.post("/bookings")
async def create_booking(
    data: NewBookingInfo,
    context: QueueContext = Depends(get_cached_queue_context),
    user_id: str = Depends(get_verified_mss_user_id),
    queue_pool: QueuePool = Depends(get_queue_pool),
    db_engine: Engine = Depends(get_db_engine),
) -> Booking:
    """Creates a booking for the user of the given token

    Args:
        user_id: the MSS user_id as sent by MSS
        context: the queue context for the queues
        data: the information about the new booking
        queue_pool: the pool of queues to user
        db_engine: the SQL database to submit data to

    Returns:
        the newly created booking
    """
    user = get_user(db_engine, User.id == user_id)
    if user is None:
        # create a random user if the user does not exist
        booking.create_random_user(db_engine, user_id)

    return scheduler.submit_booking(
        context, queues=queue_pool, user_id=user_id, booking_info=data
    )


@app.post("/bookings/{booking_id}/cancel")
async def cancel_booking(
    booking_id: str,
    context: QueueContext = Depends(get_cached_queue_context),
    user_id: str = Depends(get_verified_mss_user_id),
    queue_pool: QueuePool = Depends(get_queue_pool),
    is_mss_admin: bool = Depends(get_unverified_mss_is_admin),
) -> GeneralMessage:
    """Cancels a booking of given id for the user of the given token

    Args:
        context: the queue context for the queues
        booking_id: the unique identifier of the booking to cancel
        user_id: the MSS user_id as sent by MSS
        queue_pool: the collection of queues to run the jobs on
        is_mss_admin: whether the user is an admin in MSS or not

    Returns:
        the general message object with the status
    """
    scheduler.cancel_booking(
        context,
        queues=queue_pool,
        user_id=user_id,
        booking_id=booking_id,
        is_mss_admin=is_mss_admin,
    )
    return {"status": "success", "detail": f"Booking of id {booking_id} cancelled"}


@app.get("/bookings", dependencies=[Depends(get_verified_mss_user_id)])
async def view_bookings(
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
    sort: Tuple[str, ...] = Query(default=()),
    min_start_utc: Optional[datetime] = Query(default=None),
    max_start_utc: Optional[datetime] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    db_engine: Engine = Depends(get_db_engine),
) -> PaginatedListResponse[Booking]:
    """Views all available bookings

    Args:
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.
        sort: fields to sort by; prepending "-" returns the records in descending order
        db_engine: the SQL database engine to query
        user_id: the unique identifier of the owner of the given bookings
        min_start_utc: the minimum start time in UTC
        max_start_utc: the maximum start time in UTC

    Returns:
        the paginated list of the available bookings
    """
    filters = []
    if min_start_utc is not None:
        filters.append(Booking.start_utc >= min_start_utc)
    if max_start_utc is not None:
        filters.append(Booking.start_utc <= max_start_utc)
    if user_id is not None:
        filters.append(Booking.user_id == user_id)

    sort = convert_http_sort_to_db_sort(Booking, http_sort=sort)
    data = booking.get_many_bookings(
        db_engine, *filters, skip=skip, limit=limit, sort=sort
    )
    return PaginatedListResponse(skip=skip, limit=limit, data=data)


@app.get("/bookings/config", dependencies=[Depends(get_verified_mss_user_id)])
async def view_bookings_config() -> BookingsConfig:
    """Views the configurations for the bookings service

    Returns:
        the instance of the bookings configurations
    """
    return BookingsConfig.from_settings()


@app.post("/jobs")
async def submit_job(
    context: QueueContext = Depends(get_cached_queue_context),
    upload_file: Annotated[UploadFile, Depends(validate_job_file)] = File(...),
    token_claims: MSSTokenClaims = Depends(get_mss_token_claims_dep(job_exists=False)),
    queue_pool: QueuePool = Depends(get_queue_pool),
    force_normal_queue: bool = Query(default=False),
) -> Job:
    """Receives quantum jobs to process. This can be done by any IP address

    Args:
        upload_file: the quantum job file uploaded
        token_claims: the user_id and job_id associated with this request
        queue_pool: the collection of queues to run the jobs on
        context: the queue context for the job in the queue
        force_normal_queue: whether to force the job to run on the normal queue or not

    Returns:
        the submitted job
    """
    context = copy.deepcopy(context)
    context["force_normal_queue"] = force_normal_queue

    return scheduler.submit_job_file(
        context,
        queues=queue_pool,
        upload_file=upload_file,
        credentials=token_claims,
    )


@app.get("/jobs/{job_id}")
async def view_job(
    job_id: str,
    user_id: str = Depends(get_verified_mss_user_id),
    is_mss_admin: bool = Depends(get_unverified_mss_is_admin),
    context: QueueContext = Depends(get_cached_queue_context),
) -> Job:
    """View the job of given job_id if job belongs to current user or if user is admin

    Args:
        context: the queue context for the job in the queue
        job_id: the unique identifier of the job
        user_id: the user_id as provided by MSS
        is_mss_admin: whether the user is an mss admin or not

    Returns:
        the job of the given job_id
    """
    return scheduler.get_job(
        context, job_id=job_id, user_id=user_id, is_mss_admin=is_mss_admin
    )


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    details: CancellationDetails,
    context: QueueContext = Depends(get_cached_queue_context),
    mss_auth_details: MSSAuthDetails = Depends(get_verified_mss_details),
    queue_pool: QueuePool = Depends(get_queue_pool),
) -> GeneralMessage:
    """Cancels the job of given job_id if job belongs to current user or if user is admin

    Args:
        job_id: the unique identifier of the job
        details: the extra information passed when canceling the job
        context: the queue context for the job in the queue
        mss_auth_details: the auth details from MSS for this request
        queue_pool: the collection of queues to run the jobs on

    Returns:
        a general message showing status
    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
        rq.exceptions.InvalidJobOperationError: if the job has already been cancelled
        rq.exceptions.InvalidJobOperation: if the job has already been cancelled
    """
    scheduler.cancel_job(
        context,
        queues=queue_pool,
        job_id=job_id,
        user_id=mss_auth_details.user_id,
        is_mss_admin=mss_auth_details.is_mss_admin,
        reason=details.reason,
    )
    return {"status": "success", "detail": f"Job of id {job_id} cancelled"}


@app.delete("/jobs/{job_id}")
async def remove_job(
    job_id: str,
    context: QueueContext = Depends(get_cached_queue_context),
    user_id: str = Depends(get_verified_mss_user_id),
    queue_pool: QueuePool = Depends(get_queue_pool),
    is_mss_admin: bool = Depends(get_unverified_mss_is_admin),
) -> GeneralMessage:
    """Deletes the job of given job_id if job belongs to current user or if user is admin

    Args:
        job_id: the unique identifier of the job
        context: the queue context for the job in the queue
        user_id: the JWT token for the user, transformed into user_id by callback
        queue_pool: the collection of queues to run the jobs on
        is_mss_admin: whether the user is an mss admin or not

    Returns:
        a general message showing status
    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
    """
    scheduler.delete_job(
        context,
        queues=queue_pool,
        job_id=job_id,
        user_id=user_id,
        is_mss_admin=is_mss_admin,
    )
    return {"status": "success", "detail": f"Job of id {job_id} deleted"}


@app.get("/jobs")
async def view_jobs(
    context: QueueContext = Depends(get_cached_queue_context),
    user_id: str = Depends(get_verified_mss_user_id),
    status: Optional[JobStatus] = Query(default=None),
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
) -> dict:
    """Views all jobs that belong to the current user

    Args:
        context: the queue context for the jobs in the queue
        user_id: the unique identifier of the currently logged in user
        status: the status of the jobs to return; default = None, i.e. all statuses
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.

    Returns:
        the paginated list of the available bookings
    """
    data = scheduler.get_many_jobs(
        context, user_id=user_id, status=status, skip=skip, limit=limit
    )
    results = PaginatedListResponse[Job](skip=skip, limit=limit, data=data)
    return results.model_dump(mode="json")
