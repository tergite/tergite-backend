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

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Optional

from cryptography.exceptions import InvalidSignature
from fastapi import Depends, FastAPI, UploadFile
from fastapi.requests import Request
from filelock import FileLock
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine

import settings
from services.booking import (
    MSSTokenClaims,
    get_mss_token_claims,
)
from services.devices import is_offline
from services.external.mss import (
    claim_mss_nonce,
    verify_mss_signature,
)
from services.scheduler import (
    DeviceResources,
    JobFile,
    close_device_resources,
    get_job,
    init_recalibration,
    stop_recalibration,
)
from utils.config import create_device_folders
from utils.datetime import get_utc_now
from utils.exc import (
    ConflictError,
    InvalidJobIdInUploadedFileError,
    InvalidRequestError,
    IsOfflineError,
    ItemNotFoundError,
    NotAuthenticatedError,
    UnauthorizedError,
)
from utils.logging import set_up_logging
from utils.sql_db import migrate_sql_db
from utils.strings import validate_uuid4_str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts each device this process serves before the application, and stops them after

    Args:
        app: the FastAPI application whose state gets the devices' resources
    """
    # each API worker, which uvicorn may start apart from main.py, sets up its own
    set_up_logging(settings.get_device_settings().log_level)
    app.state.device_resources = {}
    for device in settings.get_device_names():
        device_settings = settings.get_device_settings(device)
        create_device_folders(device_settings)
        # one API worker at a time, as starting a device migrates its database and
        # restarts its recalibration
        with FileLock(device_settings.start_up_lock_file):
            migrate_sql_db(
                device_settings.booking_db_url,
                device_name=device_settings.default_prefix,
            )
            resources = DeviceResources.from_settings(device_settings)
            app.state.device_resources[device] = resources
            _start_device(resources)

    logging.info(f"starting app at {get_utc_now()}")
    yield

    for device_resources in app.state.device_resources.values():
        stop_recalibration(device_resources)
    for device_resources in app.state.device_resources.values():
        close_device_resources(device_resources)
    app.state.device_resources.clear()


def get_device_resources(request: Request) -> DeviceResources:
    """Dependency injector of the resources of the device the request's path names

    Args:
        request: the current FastAPI request

    Returns:
        the resources of the device the request is for
    """
    return request.app.state.device_resources[request.state.device_name]


DeviceResourcesDep = Annotated[DeviceResources, Depends(get_device_resources)]


def get_booking_db(device: DeviceResourcesDep) -> Engine:
    """Gets the SQLAlchemy engine for the bookings service

    Args:
        device: the resources of the device the request is for

    Returns:
        the engine of the device's bookings database
    """
    return device.bookings_db


def check_device_is_online(device: DeviceResourcesDep) -> None:
    """Refuses the request while the device is switched off

    Args:
        device: the resources of the device the request is for

    Raises:
        IsOfflineError: {device name} is offline
    """
    if is_offline(device.bookings_db, device.settings.default_prefix):
        raise IsOfflineError(f"{device.settings.default_prefix} is offline")


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

    raise InvalidJobIdInUploadedFileError(
        f"The job does not have a valid UUID4 {job_id_field}"
    )


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
    `x-mss-timestamp` headers. The signature also covers `x-mss-is-admin`, the method and
    the path, so that the headers cannot be given admin or moved to another route.

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

        is_admin = request.headers.get("x-mss-is-admin", "")
        message = f"{user_id}|{nonce}|{timestamp}|{is_admin}|{request.method}|{request.url.path}"
        device = get_device_resources(request)
        verify_mss_signature(
            signature=signature,
            message=message,
            mss_public_key=device.mss_public_key,
        )

        nonce_ttl = device.settings.mss_nonce_ttl
        current_timestamp = datetime.now().timestamp()
        timestamp_float = float(timestamp)
        time_difference = current_timestamp - timestamp_float
        if time_difference > nonce_ttl:
            raise ValueError(
                f"nonce of timestamp {timestamp} is {time_difference}s older than {nonce_ttl} seconds"
            )
        elif time_difference < 0:
            raise ValueError(f"timestamp {timestamp} is in the future")

        # claimed before the route runs, so it cannot be replayed while in flight or after a crash
        is_nonce_new = claim_mss_nonce(
            device.bookings_db,
            device_name=device.settings.default_prefix,
            nonce=nonce,
            ttl=nonce_ttl,
        )
        if not is_nonce_new:
            raise ValueError(f"duplicate request nonce: '{nonce}'")

        return user_id
    except (KeyError, InvalidSignature, ValueError) as exp:
        logging.error(exp)
        raise NotAuthenticatedError("user not authenticated")


def get_mss_token_claims_dep(
    should_job_exist: bool = True,
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
        should_job_exist: whether the job is expected to exist already or not
        job_id_field: the name of the parameter or field that contains the job_id. Default is 'job_id'

    """

    def dependency_injector(
        device: DeviceResourcesDep,
        job_id: str = Depends(get_job_id_dependency(job_id_field=job_id_field)),
        token: Optional[str] = Depends(get_bearer_token),
    ) -> MSSTokenClaims:
        """Gets a valid user_id-job_id pair.

        Args:
            device: what the device runs on
            job_id: the job_id as got from the parameters or from the uploaded file
            token: the bearer token in the authorization header

        Returns:
            the MSSTokenClaims of the user_id and the job_id

        Raises:
            NotAuthenticatedError: job {job_id} does not exist for current user
            ConflictError: job {job_id} is already {auth_log.status}
            UnauthorizedError: unexpected job id {job_id}
            ItemNotFoundError: job {job_id} does not exist
            InvalidJobIdInUploadedFileError: The job does not have a valid UUID4 {job_id_field}
        """
        claims = get_mss_token_claims(token, secret_key=device.settings.jwt_secret)
        user_id = claims.user_id
        if job_id != claims.job_id:
            raise UnauthorizedError(f"unexpected job id {job_id}")

        try:
            get_job(device, job_id=job_id, user_id=user_id)
            if not should_job_exist:
                raise ConflictError(f"job {job_id} already exists")
        except NotAuthenticatedError:
            raise NotAuthenticatedError(f"job {job_id} does not exist for current user")
        except ItemNotFoundError:
            if should_job_exist:
                raise ItemNotFoundError(f"job {job_id} does not exist")

        return claims

    return dependency_injector


async def validate_job_file(upload_file: UploadFile) -> UploadFile:
    """Validates a job file input and returns the original upload file

    Validations:
    - Follows the JobFile structure

    Args:
        upload_file: the UploadFile instance that is uploaded

    Returns:
        the same upload file, rewound to its start

    Raises:
        InvalidRequestError: Invalid file: {error}
    """
    try:
        upload_file.file.seek(0)
        content = await upload_file.read()

        JobFile.model_validate_json(content)

        # optional: reset stream for reuse
        upload_file.file.seek(0)
        return upload_file
    except (ValidationError, KeyError, TypeError) as exp:
        raise InvalidRequestError(f"Invalid file: {exp}")


def get_bearer_token(request: Request) -> Optional[str]:
    """Extracts the bearer token from the request.

    It throws a 401 exception if not exist and `raise_if_error` is False

    Args:
        request: the request object from FastAPI

    Raises:
        NotAuthenticatedError: user not authenticated

    Returns:
        the bearer token as a string or None if it does not exist and `raise_if_error` is False
    """
    try:
        authorization_header = request.headers["Authorization"]
        return authorization_header.split("Bearer ")[1].strip()
    except (KeyError, IndexError):
        raise NotAuthenticatedError("user not authenticated")


class MSSAuthDetails(BaseModel):
    """The user and job that MSS's headers or the job's token verified for a request"""

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
    except NotAuthenticatedError:
        token = get_bearer_token(request)
        secret_key = get_device_resources(request).settings.jwt_secret
        claims = get_mss_token_claims(token, secret_key=secret_key)
        if claims.job_id != job_id:
            raise UnauthorizedError("forbidden")
        return MSSAuthDetails(user_id=claims.user_id, job_id=job_id)


def _start_device(device: DeviceResources) -> None:
    """Starts the device's recalibrations, the first now or, where its settings say not to
    recalibrate at start, an interval from now; its parameters are the heavy worker's to
    publish"""
    settings = device.settings
    first_at = None
    if not settings.should_recalibrate_at_start:
        first_at = get_utc_now() + timedelta(
            seconds=settings.default_recalibration_interval
        )
    init_recalibration(device, start_timestamp=first_at)
