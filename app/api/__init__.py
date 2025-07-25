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
from fastapi.responses import FileResponse, Response
from pydantic import ValidationError
from typing_extensions import Annotated

import settings
from app.services import booking, scheduler
from app.services.booking.models import (
    Booking,
    LoginDetails,
    MSSLoginDetails,
    NewBookingInfo,
    NewUserInfo,
    User,
    UserProfile,
)
from app.utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
    JobAlreadyCancelled,
    JobAlreadyCompleteError,
    MaxBookingsError,
    NotAllowedError,
    NotAuthenticatedError,
    UnauthorizedError,
)
from settings import (
    CLIENT_IP_WHITELIST,
    JOB_UPLOAD_POOL_DIRNAME,
    LOGFILE_DOWNLOAD_POOL_DIRNAME,
    STORAGE_PREFIX_DIRNAME,
    STORAGE_ROOT,
)

from ..libs import device_parameters as props_lib
from ..services.booking import get_user
from ..services.scheduler.queues import QueuePool
from ..utils.api import (
    GeneralMessage,
    PaginatedListResponse,
    TokenResponse,
    encrypt_mss_jwt_token,
    to_http_error,
)
from ..utils.queues.dtos import Job
from ..utils.redis_store import Collection, ItemNotFoundError
from .dependencies import (
    DB_ENGINE,
    get_bearer_token,
    get_redis_connection,
    get_user_id_from_token,
    get_valid_credentials_dep,
    get_whitelisted_ip,
    validate_admin_token,
    validate_job_file,
)
from .exc import InvalidJobIdInUploadedFileError, IpNotAllowedError

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
async def limit_access_to_ip_whitelist(request: Request, call_next):
    """Limits access to only the given IP addresses in the white list.

    This middleware adds the 'whitelisted_ip' property to the request.state
    if the IP of the request is in the CLIENT_IP_WHITELIST.
    Some endpoints will raise an IpNotAllowedError if the 'whitelisted_ip'
    property does not exist. Others will ignore it and work normally.

    The endpoints that raise an IpNotAllowedError are those that are
    essentially private.

    Args:
        request: the current FastAPI request object
        call_next: the callback that calls the next middleware or route handler
    """
    ip = f"{request.client.host}"

    if ip in CLIENT_IP_WHITELIST:
        request.state.whitelisted_ip = ip

    try:
        return await call_next(request)
    except IpNotAllowedError:
        # return an empty response mimicking 404
        return Response(status_code=status.HTTP_404_NOT_FOUND)


# routing


@app.post("/jobs")
async def submit_job(
    upload_file: Annotated[UploadFile, Depends(validate_job_file)] = File(...),
    credentials: MSSLoginDetails = Depends(get_valid_credentials_dep(job_exists=False)),
    force_normal_queue: bool = Query(default=False),
) -> Job:
    """Receives quantum jobs to process

    Args:
        upload_file: the quantum job file uploaded
        credentials: the user_id and job_id associated with this request
        force_normal_queue: whether to force the job to run on the normal queue or not

    Returns:
        the submitted job
    """
    return scheduler.submit_job_file(
        _QUEUE_POOL,
        upload_file=upload_file,
        upload_folder=_JOB_UPLOAD_POOL,
        credentials=credentials,
        force_normal_queue=force_normal_queue,
    )


# FIXME: Change this
@app.get("/jobs", dependencies=[Depends(get_whitelisted_ip)])
async def fetch_all_jobs(
    redis_connection: RedisDep,
):
    """Returns all available jobs

    Args:
        redis_connection: the connection to the redis database
    """
    jobs_db = Collection(redis_connection, schema=Job)
    data = jobs_db.get_all()
    # TODO: Paginate these in future
    return [item.model_dump(mode="json") for item in data]


# FIXME: Change this
@app.get("/jobs/{job_id}", dependencies=[Depends(get_valid_credentials_dep())])
async def fetch_job(redis_connection: RedisDep, job_id: str):
    """Returns a job of the given job_id"""
    jobs_db = Collection(redis_connection, schema=Job)
    job = jobs_db.get_one((job_id,))
    # TODO: Standardize the return schema here
    return {"message": job.model_dump(mode="json")}


# FIXME: Change this
@app.get("/jobs/{job_id}/status", dependencies=[Depends(get_valid_credentials_dep())])
async def fetch_job_status(redis_connection: RedisDep, job_id: str):
    """Returns the status of the given job of the given job_id

    Args:
        redis_connection: the connection to the redis database
        job_id: the unique identifier of the job
    """
    jobs_db = Collection(redis_connection, schema=Job)
    job: Job = jobs_db.get_one((job_id,))
    # TODO: Standardize the return schema here
    return {"message": job.status}


# FIXME: Change this
@app.get("/jobs/{job_id}/result", dependencies=[Depends(get_valid_credentials_dep())])
async def fetch_job_result(redis_connection: RedisDep, job_id: str):
    """Retrieves the result of the job if exists

    Args:
        redis_connection: the connection to the redis database
        job_id: the unique identifier of the job
    """
    jobs_db = Collection(redis_connection, schema=Job)
    job: Job = jobs_db.get_one((job_id,))
    if job.result is not None:
        # TODO: Standardize the return schema here
        return {"message": job.result.model_dump(mode="json")}

    # FIXME: this does not communicate well when the job has failed
    return {"message": "job has not finished"}


# FIXME: Change this
@app.delete("/jobs/{job_id}", dependencies=[Depends(get_valid_credentials_dep())])
async def remove_job(redis_connection: RedisDep, job_id: str):
    """Deletes the job of the given job_id

    Args:
        redis_connection: the connection to the redis database
        job_id: the unique identifier of the job
    """
    try:
        jobs_service.cancel_job(redis_connection, job_id=job_id, reason="deleting job")
    except JobAlreadyCancelled:
        pass

    jobs_db = Collection(redis_connection, schema=Job)
    jobs_db.delete_many([(job_id,)])
    return {"message": f"job {job_id} not found"}


# FIXME: Change this
@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(get_valid_credentials_dep())])
async def cancel_job(
    redis_connection: RedisDep, job_id: str, reason: str = Body("", embed=False)
):
    """Cancels a given job's processing

    Args:
        redis_connection: the connection to the redis database
        job_id: the unique identifier of the job
        reason: reason for cancelling the job
    """
    print(f"Cancelling job {job_id}")
    jobs_service.cancel_job(redis_connection, job_id=job_id, reason=reason)


# FIXME: Change this
@app.get(
    "/logfiles/{logfile_id}",
    dependencies=[Depends(get_valid_credentials_dep(job_id_field="logfile_id"))],
)
async def download_logfile(logfile_id: UUID):
    """Downloads the job logfile

    Args:
        logfile_id: the id of the logfile usually the job id
    """
    file = (_LOG_FILE_POOL / str(logfile_id)).with_suffix(".hdf5")
    if file.exists():
        return FileResponse(file)
    return {"message": "logfile not found"}


@app.get("/static-properties", dependencies=[Depends(get_whitelisted_ip)])
async def get_static_properties(redis_connection: RedisDep):
    """Retrieves the device properties that are not changing"""
    return props_lib.get_device_info(redis_connection)


@app.get("/dynamic-properties", dependencies=[Depends(get_whitelisted_ip)])
async def get_dynamic_properties(redis_connection: RedisDep):
    """Retrieves the device properties that are changing with time i.e. calibration data"""
    return props_lib.get_device_calibration_info(redis_connection)


# FIXME: From the scheduler code
@app.get("/", dependencies=[Depends(get_whitelisted_ip)])
async def root():
    return {"message": "Welcome to BCC machine"}


@app.get("/me")
async def view_profile(
    user_id: str = Depends(get_user_id_from_token),
) -> UserProfile:
    """Views the profile of the current user

    Args:
        user_id: the JWT token for the user, transformed into user_id by callback

    Raises:
        ItemNotFoundError (404): user not found

    Returns:
        the user profile of the current user
    """
    return booking.get_user_profile(DB_ENGINE, user_id)


@app.delete("/me")
async def delete_profile(
    user_id: str = Depends(get_user_id_from_token),
) -> GeneralMessage:
    """Deletes the profile of the current user

    Args:
        user_id: the JWT token for the user, transformed into user_id by callback

    Raises:
        ItemNotFoundError (404): user not found

    Returns:
        A general message object with status
    """
    scheduler.delete_user_profile(_QUEUE_POOL, user_id)
    return GeneralMessage(status="success", detail="Profile deleted")


@app.post("/login")
async def login(data: LoginDetails) -> TokenResponse:
    """Logs in the user of the given name and email and returns a JWT token

    Args:
        data: the login data

    Returns:
        A token response containing access token
    """
    user = booking.authenticate_user(DB_ENGINE, data=data)
    token = booking.create_jwt_token(user)
    return {"access_token": token, "token_type": "bearer"}


@app.post("/mss-login", dependencies=[Depends(get_whitelisted_ip)])
async def mss_login(body: MSSLoginDetails) -> TokenResponse:
    """Logs in a user via MSS.

    This is a special route as it can only be used through the associated MSS instance.
    On top of that MSS being whitelisted, the public key of that MSS will be used to encrypt
    the access token that will be sent over to the user.

    Take note that the JWT must also contain the `user_id` as well as the `job_id`
    as these will be used to ensure a JWT is not used for another job.

    Take note also that if the given user does not exist, the user is created with
    a random email and password.
    """
    user_id = body.user_id
    job_id = body.job_id

    user = get_user(DB_ENGINE, User.id == user_id)
    if user is None:
        # create a random user if the user does not exist
        user = booking.create_random_user(DB_ENGINE, user_id)

    # create encrypted token
    token = booking.create_mss_jwt_token(user, job_id=job_id)
    encrypted_token = encrypt_mss_jwt_token(token)
    return {"access_token": encrypted_token, "token_type": "bearer"}


@app.post("/root-user")
async def create_root_user(data: NewUserInfo) -> UserProfile:
    """Creates the root user given the name and email, only if none exists, else errors

    Args:
        data: the information about the new user

    Raises:
        NotAllowedError (405): root user already exists

    Returns:
        the created user
    """
    user = booking.create_root_user(DB_ENGINE, data=data)
    return UserProfile.from_user(user)


@app.post("/users")
async def create_user(data: NewUserInfo) -> UserProfile:
    """Creates a user given the name and email

    Args:
        data: the information about the new user

    Returns:
        the created user
    """
    user = booking.create_user(DB_ENGINE, data=data)
    return UserProfile.from_user(user)


@app.delete("/users/{user_id}")
async def remove_user(
    user_id: str,
    _current_user_id: str = Depends(validate_admin_token),
) -> GeneralMessage:
    """Deletes the user of the given user_id

    Only admins are allowed to remove users

    Args:
        user_id: the unique identifier of the user
        _current_user_id: the JWT token for the user, transformed into current_user_id by callback

    Raises:
        ItemNotFoundError: user not found

    Returns:
        A general message object with status
    """
    scheduler.delete_user_profile(_QUEUE_POOL, user_id)
    return GeneralMessage(status="success", detail="User deleted")


@app.get("/users")
async def view_users(
    _user_id: str = Depends(validate_admin_token),
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
) -> PaginatedListResponse[UserProfile]:
    """Views all users

    Args:
        _user_id: the JWT token for the user, transformed into user_id by callback
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
    user_id: str = Depends(get_user_id_from_token),
) -> Booking:
    """Creates a booking for the user of the given token

    Args:
        user_id: the JWT token for the user, transformed into user_id by callback
        data: the information about the new booking

    Returns:
        the newly created booking
    """
    return scheduler.submit_booking(_QUEUE_POOL, user_id, booking_info=data)


@app.post("/bookings/{booking_id}/cancel")
async def cancel_booking(
    booking_id: str,
    user_id: str = Depends(get_user_id_from_token),
) -> GeneralMessage:
    """Cancels a booking of given id for the user of the given token

    Args:
        booking_id: the unique identifier of the booking to cancel
        user_id: the JWT token for the user, transformed into user_id by callback

    Returns:
        the general message object with the status
    """
    scheduler.cancel_booking(_QUEUE_POOL, user_id=user_id, booking_id=booking_id)
    return {"status": "success", "detail": f"Booking of id {booking_id} cancelled"}


@app.get("/bookings")
async def view_bookings(
    _user_id: str = Depends(get_user_id_from_token),
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
) -> PaginatedListResponse[Booking]:
    """Views all available bookings

    Args:
        _user_id: the JWT token for the user, transformed into user_id by callback
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.

    Returns:
        the paginated list of the available bookings
    """
    data = booking.get_many_bookings(DB_ENGINE, skip=skip, limit=limit)
    return PaginatedListResponse(skip=skip, limit=limit, data=data)


@app.get("/jobs/{job_id}")
async def view_job(
    job_id: str,
    user_id: str = Depends(get_user_id_from_token),
) -> Job:
    """View the job of given job_id if job belongs to current user or if user is admin

    Args:
        job_id: the unique identifier of the job
        user_id: the JWT token for the user, transformed into user_id by callback

    Returns:
        the job of the given job_id
    """
    return scheduler.get_job(job_id, user_id=user_id)


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user_id: str = Depends(get_user_id_from_token),
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
