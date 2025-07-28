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
from typing import Optional
from uuid import UUID

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Query,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import FileResponse
from pydantic import ValidationError
from typing_extensions import Annotated

import settings
from app.services import booking, scheduler
from app.services.booking.models import (
    Booking,
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
    JobAlreadyCancelled,
    JobAlreadyCompleteError,
    MaxBookingsError,
    NotAllowedError,
    NotAuthenticatedError,
    UnauthorizedError,
)
from settings import (
    JOB_UPLOAD_POOL_DIRNAME,
    LOGFILE_DOWNLOAD_POOL_DIRNAME,
    STORAGE_PREFIX_DIRNAME,
    STORAGE_ROOT,
)

from ..libs import device_parameters as props_lib
from ..libs.queues.dtos import Job
from ..services.booking import get_user
from ..services.scheduler.queues import QueuePool
from ..utils.api import (
    GeneralMessage,
    PaginatedListResponse,
    RequestLog,
    TokenResponse,
    encrypt_mss_jwt_token,
    get_request_logs_store,
    to_http_error,
)
from ..utils.redis_store import ItemNotFoundError
from ..utils.strings import uuid_str
from .dependencies import (
    DB_ENGINE,
    get_user_job_id_pair_dep,
    get_verified_mss_admin_user_id,
    get_verified_mss_user_id,
    validate_job_file,
)

_JOB_UPLOAD_POOL = STORAGE_ROOT / STORAGE_PREFIX_DIRNAME / JOB_UPLOAD_POOL_DIRNAME
_LOG_FILE_POOL = STORAGE_ROOT / STORAGE_PREFIX_DIRNAME / LOGFILE_DOWNLOAD_POOL_DIRNAME


# application
app = FastAPI(
    title="Backend Control Computer",
    description="Interfaces Quantum processor via REST API",
    version="2025.06.2",
)

_QUEUE_POOL = QueuePool.from_settings()

# exception handlers
app.add_exception_handler(NotAllowedError, to_http_error(405))
app.add_exception_handler(NotAuthenticatedError, to_http_error(401))
app.add_exception_handler(UnauthorizedError, to_http_error(403))
app.add_exception_handler(ItemNotFoundError, to_http_error(404))
app.add_exception_handler(JobAlreadyCompleteError, to_http_error(406))
app.add_exception_handler(ConflictError, to_http_error(400))
app.add_exception_handler(MaxBookingsError, to_http_error(400))
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
    dependencies=[Depends(get_user_job_id_pair_dep(job_id_field="logfile_id"))],
)
async def download_logfile(logfile_id: UUID):
    """Downloads the job logfile

    Args:
        logfile_id: the id of the logfile usually the job id

    Raises:
        ItemNotFoundError: logfile {logfile_id} not found
    """
    file = (_LOG_FILE_POOL / str(logfile_id)).with_suffix(".hdf5")
    if file.exists():
        return FileResponse(file)
    raise ItemNotFoundError(f"logfile {logfile_id} not found")


@app.get("/static-properties", dependencies=[Depends(get_verified_mss_user_id)])
async def get_static_properties():
    """Retrieves the device properties that are not changing"""
    return props_lib.get_device_info()


@app.get("/dynamic-properties", dependencies=[Depends(get_verified_mss_user_id)])
async def get_dynamic_properties():
    """Retrieves the device properties that are changing with time i.e. calibration data"""
    return props_lib.get_device_calibration_info()


@app.get("/me")
async def view_profile(user_id: str = Depends(get_verified_mss_user_id)) -> UserProfile:
    """Views the profile of the current user

    Args:
        user_id: the user_id as submitted by MSS

    Raises:
        ItemNotFoundError (404): user not found

    Returns:
        the user profile of the current user
    """
    return booking.get_user_profile(DB_ENGINE, user_id)


@app.delete("/me")
async def delete_profile(
    user_id: str = Depends(get_verified_mss_user_id),
) -> GeneralMessage:
    """Deletes the profile of the current user

    Args:
        user_id: the user_id as submitted by MSS

    Raises:
        ItemNotFoundError (404): user not found

    Returns:
        A general message object with status
    """
    scheduler.delete_user_profile(_QUEUE_POOL, user_id)
    return GeneralMessage(status="success", detail="Profile deleted")


@app.post("/token")
async def get_mss_token(
    body: MSSTokenClaims, mss_user_id: str = Depends(get_verified_mss_user_id)
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

    user = get_user(DB_ENGINE, User.id == user_id)
    if user is None:
        # create a random user if the user does not exist
        user = booking.create_random_user(DB_ENGINE, user_id)

    # create encrypted token
    token = booking.create_mss_jwt_token(user, job_id=job_id)
    encrypted_token = encrypt_mss_jwt_token(token)
    return {"access_token": encrypted_token, "token_type": "bearer"}


@app.post("/users", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def create_user(data: NewUserInfo) -> UserProfile:
    """Creates a user given the name and email

    Only MSS admin users can create users here

    Args:
        data: the information about the new user

    Returns:
        the created user
    """
    user = booking.create_user(DB_ENGINE, data=data)
    return UserProfile.from_user(user)


@app.delete("/users/{user_id}", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def remove_user(user_id: str) -> GeneralMessage:
    """Deletes the user of the given user_id

    Only admins are allowed to remove users

    Args:
        user_id: the unique identifier of the user

    Raises:
        ItemNotFoundError: user not found

    Returns:
        A general message object with status
    """
    scheduler.delete_user_profile(_QUEUE_POOL, user_id)
    return GeneralMessage(status="success", detail="User deleted")


@app.get("/users", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def view_users(
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
) -> PaginatedListResponse[UserProfile]:
    """Views all users

    Only MSS admin users can view this

    Args:
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.

    Returns:
        the paginated list of the available bookings
    """
    data = booking.get_many_user_profiles(DB_ENGINE, skip=skip, limit=limit)
    return PaginatedListResponse(skip=skip, limit=limit, data=data)


@app.post("/bookings")
async def create_booking(
    data: NewBookingInfo,
    user_id: str = Depends(get_verified_mss_user_id),
) -> Booking:
    """Creates a booking for the user of the given token

    Args:
        user_id: the MSS user_id as sent by MSS
        data: the information about the new booking

    Returns:
        the newly created booking
    """
    return scheduler.submit_booking(_QUEUE_POOL, user_id, booking_info=data)


@app.post("/bookings/{booking_id}/cancel")
async def cancel_booking(
    booking_id: str,
    user_id: str = Depends(get_verified_mss_user_id),
) -> GeneralMessage:
    """Cancels a booking of given id for the user of the given token

    Args:
        booking_id: the unique identifier of the booking to cancel
        user_id: the MSS user_id as sent by MSS

    Returns:
        the general message object with the status
    """
    scheduler.cancel_booking(_QUEUE_POOL, user_id=user_id, booking_id=booking_id)
    return {"status": "success", "detail": f"Booking of id {booking_id} cancelled"}


@app.get("/bookings", dependencies=[Depends(get_verified_mss_user_id)])
async def view_bookings(
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
) -> PaginatedListResponse[Booking]:
    """Views all available bookings

    Args:
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.

    Returns:
        the paginated list of the available bookings
    """
    data = booking.get_many_bookings(DB_ENGINE, skip=skip, limit=limit)
    return PaginatedListResponse(skip=skip, limit=limit, data=data)


@app.post("/jobs")
async def submit_job(
    upload_file: Annotated[UploadFile, Depends(validate_job_file)] = File(...),
    token_claims: MSSTokenClaims = Depends(get_user_job_id_pair_dep(job_exists=False)),
    force_normal_queue: bool = Query(default=False),
) -> Job:
    """Receives quantum jobs to process. This can be done by any IP address

    Args:
        upload_file: the quantum job file uploaded
        token_claims: the user_id and job_id associated with this request
        force_normal_queue: whether to force the job to run on the normal queue or not

    Returns:
        the submitted job
    """
    return scheduler.submit_job_file(
        _QUEUE_POOL,
        upload_file=upload_file,
        upload_folder=_JOB_UPLOAD_POOL,
        credentials=token_claims,
        force_normal_queue=force_normal_queue,
    )


@app.get("/jobs/{job_id}")
async def view_job(
    job_id: str,
    user_id: str = Depends(get_verified_mss_user_id),
) -> Job:
    """View the job of given job_id if job belongs to current user or if user is admin

    Args:
        job_id: the unique identifier of the job
        user_id: the user_id as provided by MSS

    Returns:
        the job of the given job_id
    """
    return scheduler.get_job(job_id, user_id=user_id)


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user_id: str = Depends(get_verified_mss_user_id),
) -> GeneralMessage:
    """Cancels the job of given job_id if job belongs to current user or if user is admin

    Args:
        job_id: the unique identifier of the job
        user_id: the JWT token for the user, transformed into user_id by callback

    Returns:
        a general message showing status
    """
    scheduler.cancel_job(_QUEUE_POOL, job_id=job_id, user_id=user_id)
    return {"status": "success", "detail": f"Booking of id {job_id} cancelled"}
