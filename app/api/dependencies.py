# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2023
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Dependencies useful for the FastAPI API"""
import dataclasses
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from os import PathLike
from typing import Optional, Tuple, Unpack

from cryptography.exceptions import InvalidSignature
from fastapi import Depends, FastAPI, HTTPException, UploadFile, status
from fastapi.requests import Request
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, ValidationError
from redis import Redis
from sqlalchemy import Engine

import settings

from ..libs.device_parameters import BackendConfig, save_all_device_params
from ..libs.quantum_executor.base.executor import QuantumExecutor
from ..libs.queues.dtos import ExecutorOptions, JobFile, QueueContext
from ..services.booking.models import MSSTokenClaims
from ..services.booking.service import get_user_job_id_pair_from_token
from ..services.booking.store import get_bookings_sql_engine
from ..services.external.mss.service import AsyncMssClient, AsyncMssClientPipe
from ..services.scheduler import get_job
from ..services.scheduler.queues import QueuePool
from ..services.scheduler.utils import (
    init_executor,
)
from ..utils.api import get_request_logs_store, verify_mss_signature
from ..utils.datetime import get_utc_now
from ..utils.exc import (
    ConflictError,
    InvalidJobIdInUploadedFileError,
    ItemNotFoundError,
    NotAuthenticatedError,
    UnauthorizedError,
)
from ..utils.redis import get_redis_connection
from ..utils.strings import validate_uuid4_str

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
DB_ENGINE = get_bookings_sql_engine(settings.BOOKING_DB_URL)
QUEUE_POOL = QueuePool.from_settings()
QUEUE_CONTEXT: Optional[QueueContext] = None
_REDIS_CONNECTION: Optional[Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles functions to run before and after the application"""
    global DB_ENGINE, QUEUE_POOL, QUEUE_CONTEXT, _REDIS_CONNECTION

    DB_ENGINE = get_bookings_sql_engine(settings.BOOKING_DB_URL)
    QUEUE_POOL = QueuePool.from_settings()
    executor, executor_options = _get_executor_and_options(
        executor_type=settings.EXECUTOR_TYPE,
        backend_config_file=settings.BACKEND_SETTINGS,
        calibration_seed_file=settings.CALIBRATION_SEED,
        quantify_config_file=settings.QUANTIFY_CONFIG_FILE,
        quantify_metadata_file=settings.QUANTIFY_METADATA_FILE,
        should_restore_currents=settings.SHOULD_RESTORE_CURRENTS,
    )
    QUEUE_CONTEXT = {
        "queue_prefix": settings.DEFAULT_PREFIX,
        "booking_db_url": settings.BOOKING_DB_URL,
        "jobs_store_url": settings.RQ_REDIS_URL,
        "force_normal_queue": False,
        "max_idle_time": settings.MAX_IDLE_TIME,
        "is_async": settings.IS_ASYNC,
        "postprocessing_folder": f"{settings.LOG_FILE_POOL}",
        "preprocessing_folder": f"{settings.PREPROCESSED_JOB_POOL}",
        "job_upload_folder": f"{settings.JOB_UPLOAD_POOL}",
        "executor_options": executor_options,
        "preprocessing_timeout": settings.MAX_PREPROCESSING_TIME,
        "execution_timeout": settings.MAX_EXECUTION_TIME,
        "postprocessing_timeout": settings.MAX_POSTPROCESSING_TIME,
        "general_queue_timeout": settings.MAX_GENERAL_QUEUE_TIME,
    }

    with get_redis_connection(settings.RQ_REDIS_URL, is_async=False) as redis_conn:
        _REDIS_CONNECTION = redis_conn

        async with AsyncMssClient() as mss_client:
            app.state.MSS_CLIENT = mss_client
            async with AsyncMssClientPipe() as mss_client_pipe:
                await save_all_device_params(
                    redis=redis_conn,
                    backend_config=executor_options.backend_config,
                    mss_client_pipe=mss_client_pipe,
                )
            print(f"starting app at {get_utc_now()}")
            yield

            DB_ENGINE = None
            executor.close()


def get_cached_redis_connection() -> Redis:
    """Dependency injector to get the redis database connection"""
    return _REDIS_CONNECTION


def get_backend_name() -> str:
    """Dependency injector to get the backend name"""
    return QUEUE_CONTEXT["executor_options"].backend_name


def get_queue_pool() -> QueuePool:
    """Dependency injector to retrieve the latest queue pool"""
    return QUEUE_POOL


def get_db_engine() -> Engine:
    """Dependency injector to retrieve the latest sql db engine"""
    return DB_ENGINE


def get_cached_queue_context() -> QueueContext:
    """Dependency injector to retrieve the cached queue context"""
    return QUEUE_CONTEXT


def get_job_id_dependency(job_id_field: str):
    """Creates a job_id dependency injector

    Args:
        job_id_field: the name of the parameter or json field that contains the job_id
    """

    async def get_job_id(request: Request) -> str:
        """Returns the job_id either got from the params or from the uploaded file name

        Args:
            request: the FastAPI request object

        Returns:
            the valid job_id

        Raises:
            InvalidJobIdInUploadedFileError: f"The job does not have a valid UUID4 {job_id_field}"
        """
        try:
            return request.path_params[job_id_field]
        except KeyError:
            return await get_job_id_from_uploaded_file(
                request, job_id_field=job_id_field
            )

    return get_job_id


async def get_job_id_from_uploaded_file(
    request: Request, job_id_field: str
) -> Optional[str]:
    """Extracts job_id from the uploaded file

    Args:
        request: the FastAPI request object
        job_id_field: the name of key that has the job_id

    Returns:
        the job_id in the file or None if it is invalid or does not exist

    Raises:
        InvalidJobIdInUploadedFileError: f"The job does not have a valid UUID4 {job_id_field}"
    """
    try:
        form = await request.form()
        upload_file: UploadFile = form["upload_file"]
        job_dict = json.load(upload_file.file)

        job_id = job_dict[job_id_field]
        if validate_uuid4_str(job_id):
            return job_id
    except KeyError:
        pass

    error_message = f"The job does not have a valid UUID4 {job_id_field}"
    print(error_message)
    raise InvalidJobIdInUploadedFileError(error_message)


def get_verified_mss_admin_user_id(request: Request) -> str:
    """Returns the user_id if user is admin in MSS as got from MSS passed through special headers

    We are choosing to trust MSS and so whenever a request comes from
    MSS. The `x-mss-is-admin` header shows whether this is user is admin in MSS or not

    Args:
        request: the current FastAPI request

    Returns:
        the user_id as got from MSS
    """
    user_id = get_verified_mss_user_id(request)
    try:
        is_admin = request.headers["x-mss-is-admin"]
        if is_admin.lower().strip() != "true":
            raise ValueError(f"{is_admin} is not true")
    except (KeyError, ValueError) as exp:
        logging.error(exp)
        raise UnauthorizedError("Forbidden")

    return user_id


def get_unverified_mss_is_admin(request: Request) -> bool:
    """Gets the MSS flag that is-admin, without verifying

    Args:
        request: the current FastAPI request

    Returns:
        the tru if is-admin is true else false
    """
    try:
        is_admin = request.headers["x-mss-is-admin"]
        return is_admin.lower().strip() == "true"
    except (KeyError, ValueError):
        return False


def get_verified_mss_user_id(request: Request) -> str:
    """Returns the user_id as got from MSS passed through special headers

    We are choosing to trust MSS and so whenever a request comes from
    MSS, there will be an `x-mss-user-id` and `x-mss-signature` header.
    We will get the `x-mss-user-id` and return it only if the `x-mss-signature`
    is verified by MSS public key.

    For better security against replay attacks, we also use the `x-mss-request-id` and
    `x-mss-timestamp` headers

    Note that the user can be "" i.e. no user especially where there is no user expected

    Args:
        request: the current FastAPI request

    Returns:
        the user_id as got from MSS

    Raises:
        NotAuthenticatedError: user not authenticated
    """
    try:
        user_id = request.headers["x-mss-user-id"]
        nonce = request.headers["x-mss-request-id"]
        timestamp = request.headers["x-mss-timestamp"]
        signature = request.headers["x-mss-signature"]

        message = f"{user_id}-{nonce}-{timestamp}"
        verify_mss_signature(signature=signature, message=message)

        current_timestamp = datetime.now().timestamp()
        timestamp_float = float(timestamp)
        time_difference = current_timestamp - timestamp_float
        if time_difference > settings.MSS_NONCE_TTL:
            raise ValueError(
                f"nonce of timestamp {timestamp} is {time_difference}s older than {settings.MSS_NONCE_TTL} seconds"
            )
        elif time_difference < 0:
            raise ValueError(f"timestamp {timestamp} is in the future")

        requests_store = get_request_logs_store()
        if requests_store.exists(nonce):
            raise ValueError(f"duplicate request nonce: '{nonce}'")

        return user_id
    except (KeyError, InvalidSignature, ValueError) as exp:
        logging.error(exp)
        raise NotAuthenticatedError("user not authenticated")


def get_mss_token_claims_dep(
    job_exists: bool = True,
    job_id_field: str = "job_id",
):
    """Gets a dependency injector that gets the pair (user_id, job_id) from the request to allow job submission.

    It extracts the JWT token from the headers and the job_id from the parameters
    or from the file body.
    The dependency injector raises authentication or authorization errors if no
    valid token and job_id pair is found.

    The dependency injector returns the MSSTokenClaims and may raise the following errors:
        NotAuthenticatedError: job {job_id} does not exist for current user
        UnauthorizedError: job {job_id} is already {auth_log.status}
        UnauthorizedError: unexpected job id {job_id}
        InvalidJobIdInUploadedFileError: The job does not have a valid UUID4 {job_id_field}

    Args:
        job_exists: whether the job is expected to exist already or not
        job_id_field: the name of the parameter or field that contains the job_id. Default is 'job_id'

    """

    def dependency_injector(
        context: QueueContext = Depends(get_cached_queue_context),
        job_id: str = Depends(get_job_id_dependency(job_id_field=job_id_field)),
        token: Optional[str] = Depends(get_bearer_token),
    ) -> MSSTokenClaims:
        """Gets a valid user_id-job_id pair.

        Args:
            context: the of the queues running the jobs
            job_id: the job_id as got from the parameters or from the uploaded file
            token: the bearer token in the authorization header

        Returns:
            the MSSTokenClaims of the user_id and the job_id

        Raises:
            NotAuthenticatedError: job {job_id} does not exist for current user
            ConflictError: job {job_id} is already {auth_log.status}
            UnauthorizedError: unexpected job id {job_id}
            InvalidJobIdInUploadedFileError: The job does not have a valid UUID4 {job_id_field}
        """
        user_id, token_job_id = get_user_job_id_pair_from_token(token)
        if job_id != token_job_id:
            raise UnauthorizedError(f"unexpected job id {job_id}")

        try:
            get_job(context, job_id=job_id, user_id=user_id)
            if not job_exists:
                raise ConflictError(f"job {job_id} already exists")
        except NotAuthenticatedError:
            raise NotAuthenticatedError(f"job {job_id} does not exist for current user")
        except ItemNotFoundError:
            if job_exists:
                raise UnauthorizedError(f"job {job_id} does not exist")

        return MSSTokenClaims(user_id=user_id, job_id=job_id)

    return dependency_injector


async def validate_job_file(upload_file: UploadFile) -> UploadFile:
    """Validates a job file input and returns the original upload file

    Validations:
    - Follows the JobFile structure

    Args:
        upload_file: the UploadFile instance that is uploaded
    """
    try:
        upload_file.file.seek(0)
        content = await upload_file.read()

        JobFile.model_validate_json(content)

        # optional: reset stream for reuse
        upload_file.file.seek(0)
        return upload_file
    except (ValidationError, KeyError, TypeError) as exp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid file: {exp}"
        )


def get_bearer_token(request: Request) -> Optional[str]:
    """Extracts the bearer token from the request.

    It throws a 401 exception if not exist and `raise_if_error` is False

    Args:
        request: the request object from FastAPI

    Raises:
        HTTPException: Unauthorized

    Returns:
        the bearer token as a string or None if it does not exist and `raise_if_error` is False
    """
    try:
        authorization_header = request.headers["Authorization"]
        return authorization_header.split("Bearer ")[1].strip()
    except (KeyError, IndexError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user not authenticated"
        )


class MSSAuthDetails(BaseModel):
    user_id: str
    job_id: str
    is_mss_admin: bool = False


def get_verified_mss_details(request: Request) -> MSSAuthDetails:
    """Gets the MSS verified details either from token or verified MSS headers or fails with error

    Args:
        request: the request object

    Returns:
        the MSS details from the given request

    Raises:
        NotAuthenticatedError: not authenticated
        UnauthorizedError: forbidden
    """
    job_id = request.path_params["job_id"]
    try:
        user_id = get_verified_mss_user_id(request)
        is_mss_admin = get_unverified_mss_is_admin(request)
        return MSSAuthDetails(user_id=user_id, job_id=job_id, is_mss_admin=is_mss_admin)
    except NotAuthenticatedError as exp:
        token = get_bearer_token(request)
        user_id, token_job_id = get_user_job_id_pair_from_token(token)
        if token_job_id != job_id:
            raise UnauthorizedError("forbidden")
        return MSSAuthDetails(user_id=user_id, job_id=job_id)


def _get_executor_and_options(
    backend_config_file: PathLike,
    calibration_seed_file: PathLike,
    **kwargs: Unpack[ExecutorOptions],
) -> Tuple[QuantumExecutor, ExecutorOptions]:
    """Gets the executor and its options that will be passed around in the queue

    Args:
        backend_config_file: the path to the general backend configuration file
        calibration_seed_file: the path to the calibration seed file
        kwargs: keyword arguments to pass to the executor options

    Returns:
        the executor and executor options constructed from the above settings
    """
    initial_backend_config = BackendConfig.from_toml(
        backend_config_file, seed_file=calibration_seed_file
    )
    executor_options = ExecutorOptions(
        backend_name=initial_backend_config.name,
        backend_config=initial_backend_config,
        calibration_seed_file=calibration_seed_file,
        **kwargs,
    )
    executor = init_executor(executor_options, reset=True)
    # update the backend_config with the updated version got from the executor
    executor_options = dataclasses.replace(
        executor_options, backend_config=executor.backend_config
    )
    return executor, executor_options
