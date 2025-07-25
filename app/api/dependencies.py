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
from typing import Annotated, Optional, Tuple

from fastapi import Depends, HTTPException, UploadFile, status
from fastapi.requests import Request
from fastapi.security import OAuth2PasswordBearer
from pydantic import ValidationError
from redis import Redis

import settings

from ..services import booking
from ..services.booking.models import MSSLoginDetails, User
from ..services.booking.service import get_user_job_id_pair_from_token
from ..services.booking.store import get_bookings_sql_engine
from ..services.jobs.dtos import JobFile, JobStatus
from ..services.scheduler import get_job
from ..utils.exc import ItemNotFoundError, NotAuthenticatedError, UnauthorizedError
from ..utils.queues.dtos import Job
from ..utils.strings import validate_uuid4_str
from .exc import InvalidJobIdInUploadedFileError, IpNotAllowedError

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
DB_ENGINE = get_bookings_sql_engine(settings.BOOKING_DB_URL)


def get_redis_connection():
    """Returns a redis connection"""
    return settings.REDIS_CONNECTION


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


def get_whitelisted_ip(request: Request) -> str:
    """Returns the whitelisted IP if exists or raises a IpNotAllowedError

    Args:
        request: the current FastAPI request

    Returns:
        the whitelisted IP
    """
    try:
        return request.state.whitelisted_ip
    except AttributeError:
        raise IpNotAllowedError()


def get_valid_credentials_dep(
    job_exists: bool = True,
    job_id_field: str = "job_id",
):
    """Gets a dependency injector that gets the pair (user_id, job_id) for the given request.

    It extracts the JWT token from the headers and the job_id from the parameters
    or from the file body.
    The dependency injector raises authentication or authorization errors if no
    valid token and job_id pair is found.

    The dependency injector returns the MSSLoginDetails and may raise the following errors:
        NotAuthenticatedError: job {job_id} does not exist for current user
        UnauthorizedError: job {job_id} is already {auth_log.status}
        UnauthorizedError: unexpected job id {job_id}
        InvalidJobIdInUploadedFileError: The job does not have a valid UUID4 {job_id_field}

    Args:
        job_exists: whether the job is expected to exist already or not
        job_id_field: the name of the parameter or field that contains the job_id. Default is 'job_id'

    """

    def dependency_injector(
        job_id: str = Depends(get_job_id_dependency(job_id_field=job_id_field)),
        token_user_job_id_pair: Tuple[str, str] = Depends(
            get_user_job_id_pair_from_token
        ),
    ) -> MSSLoginDetails:
        """Gets a valid user_id-job_id pair.

        Args:
            job_id: the job_id as got from the parameters or from the uploaded file
            token_user_job_id_pair: the user_id, job_id pair got from the token

        Returns:
            the MSSLoginDetails of the user_id and the job_id

        Raises:
            NotAuthenticatedError: job {job_id} does not exist for current user
            UnauthorizedError: job {job_id} is already {auth_log.status}
            UnauthorizedError: unexpected job id {job_id}
            InvalidJobIdInUploadedFileError: The job does not have a valid UUID4 {job_id_field}
        """
        user_id, token_job_id = token_user_job_id_pair
        if job_id != token_job_id:
            raise UnauthorizedError(f"unexpected job id {job_id}")

        try:
            get_job(job_id=job_id, user_id=user_id)
            if not job_exists:
                raise UnauthorizedError(f"job {job_id} already exists")
            return MSSLoginDetails(user_id=user_id, job_id=job_id)
        except NotAuthenticatedError:
            raise NotAuthenticatedError(f"job {job_id} does not exist for current user")
        except ItemNotFoundError:
            if job_exists:
                raise UnauthorizedError(f"job {job_id} does not exist")

    return dependency_injector


def get_bearer_token(
    request: Request, raise_if_error: bool = settings.IS_AUTH_ENABLED
) -> Optional[str]:
    """Extracts the bearer token from the request.

    It throws a 401 exception if not exist and `raise_if_error` is False

    Args:
        request: the request object from FastAPI
        raise_if_error: whether an error should be raised if it occurs.
            defaults to settings.IS_AUTH_ENABLED

    Raises:
        HTTPException: Unauthorized

    Returns:
        the bearer token as a string or None if it does not exist and `raise_if_error` is False
    """
    try:
        authorization_header = request.headers["Authorization"]
        return authorization_header.split("Bearer ")[1].strip()
    except (KeyError, IndexError):
        if raise_if_error:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


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


async def get_user_id_from_token(token: Annotated[str, Depends(oauth2_scheme)]) -> str:
    """Returns the user id for the given token

    Args:
        token: the user token that is to be authenticated

    Returns:
        the user id for associated with the given token

    Raises:
        NotAuthenticatedError: not authenticated
    """
    return booking.get_current_user_id(token, secret_key=settings.JWT_SECRET)


async def validate_admin_token(token: Annotated[str, Depends(oauth2_scheme)]) -> str:
    """Returns the user id if the user is admin for the given token

    Args:
        token: the user token that is to be authorized

    Returns:
        the user id for associated with the given token

    Raises:
        UnauthorizedError: unauthorized
        NotAuthenticatedError: not authenticated
    """
    user_id = booking.get_current_user_id(token, secret_key=settings.JWT_SECRET)
    user = booking.get_admin_user(DB_ENGINE, User.id == user_id)
    return user.id
