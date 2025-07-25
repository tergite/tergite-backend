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
import time
from typing import Optional, Tuple

from cryptography.exceptions import InvalidSignature
from fastapi import Depends, HTTPException, UploadFile, status
from fastapi.requests import Request
from fastapi.security import OAuth2PasswordBearer
from pydantic import ValidationError

import settings

from ..services.booking.models import MSSTokenClaims
from ..services.booking.service import get_user_job_id_pair_from_token
from ..services.booking.store import get_bookings_sql_engine
from ..services.scheduler import get_job
from ..utils.api import get_request_logs_store, verify_mss_signature
from ..utils.exc import (
    InvalidJobIdInUploadedFileError,
    ItemNotFoundError,
    NotAuthenticatedError,
    UnauthorizedError,
)
from ..utils.queues.dtos import JobFile
from ..utils.strings import validate_uuid4_str

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")
DB_ENGINE = get_bookings_sql_engine(settings.BOOKING_DB_URL)


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


def get_verified_mss_user_id(request: Request) -> str:
    """Returns the user_id as got from MSS passed through special headers

    We are choosing to trust MSS and so whenever a request comes from
    MSS, there will be an `x-mss-user-id` and `x-mss-signature` header.
    We will get the `x-mss-user-id` and return it only if the `x-mss-user-id-signature`
    is verified by MSS public key.

    For better security against replay attacks, we also use the `x-mss-request-id` and
    `x-mss-timestamp` headers

    Note that the user can be "" i.e. no user especially where there is no user expected

    Args:
        request: the current FastAPI request

    Returns:
        the user_id as got from MSS
    """
    try:
        user_id = request.headers["x-mss-user-id"]
        nonce = request.headers["x-mss-request-id"]
        timestamp = request.headers["x-mss-timestamp"]
        signature = request.headers["x-mss-signature"]

        message = f"{user_id}-{nonce}-{timestamp}"
        verify_mss_signature(signature=signature, message=message)

        now = time.time()
        if abs(now - float(timestamp)) > settings.MSS_NONCE_TTL:
            raise ValueError(
                f"nonce of timestamp {timestamp} is older than {settings.MSS_NONCE_TTL} seconds"
            )

        requests_store = get_request_logs_store()
        if requests_store.exists(nonce):
            raise ValueError(f"duplicate request nonce: '{nonce}'")

        return user_id
    except (KeyError, InvalidSignature, ValueError) as exp:
        logging.error(exp)
        raise NotAuthenticatedError("user not authenticated")


def get_user_job_id_pair_dep(
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
        job_id: str = Depends(get_job_id_dependency(job_id_field=job_id_field)),
        token_user_job_id_pair: Tuple[str, str] = Depends(
            get_user_job_id_pair_from_token
        ),
    ) -> MSSTokenClaims:
        """Gets a valid user_id-job_id pair.

        Args:
            job_id: the job_id as got from the parameters or from the uploaded file
            token_user_job_id_pair: the user_id, job_id pair got from the token

        Returns:
            the MSSTokenClaims of the user_id and the job_id

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
            return MSSTokenClaims(user_id=user_id, job_id=job_id)
        except NotAuthenticatedError:
            raise NotAuthenticatedError(f"job {job_id} does not exist for current user")
        except ItemNotFoundError:
            if job_exists:
                raise UnauthorizedError(f"job {job_id} does not exist")

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
