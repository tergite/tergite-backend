# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright Martin Ahindura 2023
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Utilities to do with HTTP APIs"""
import logging
import shutil
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    List,
    Literal,
    NotRequired,
    Optional,
    TypedDict,
    TypeVar,
    Union,
)

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import HTTPException, Request, Response, UploadFile
from fastapi.exception_handlers import http_exception_handler
from pydantic import BaseModel

import settings

from .model import IncEx

ITEM = TypeVar("ITEM", bound=BaseModel)

_MSS_PUBLIC_KEY: Optional[RSAPublicKey] = None


class GeneralMessage(TypedDict):
    """A general message object sent on the API"""

    status: Literal["success", "error", "cancelled", "failed"]
    detail: NotRequired[str]


class TokenResponse(TypedDict):
    """A token response sent on the API"""

    access_token: str
    token_type: Literal["bearer",]


class PaginatedListResponse(BaseModel, Generic[ITEM]):
    """The response when sending paginated data"""

    skip: int = 0
    limit: Optional[int] = None
    data: List[ITEM] = []

    def model_dump(
        self,
        *,
        mode: Literal["json", "python"] | str = "python",
        include: IncEx | None = None,
        exclude: IncEx | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_data_none_fields: bool = True,
        round_trip: bool = False,
        warnings: bool | Literal["none", "warn", "error"] = True,
        serialize_as_any: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        return {
            "skip": self.skip,
            "limit": self.limit,
            "data": [
                item.model_dump(
                    mode=mode,
                    include=include,
                    exclude=exclude,
                    context=context,
                    by_alias=by_alias,
                    exclude_unset=exclude_unset,
                    exclude_defaults=exclude_defaults,
                    exclude_none=exclude_data_none_fields,
                    round_trip=round_trip,
                    warnings=warnings,
                    serialize_as_any=serialize_as_any,
                )
                for item in self.data
            ],
        }


def get_mss_client(app_token: str = settings.MSS_APP_TOKEN) -> requests.Session:
    """Returns an MSS client to be used to make HTTP queries to MSS

    Args:
        app_token: the app token to use when making authenticated requests

    Returns:
        the requests.Session that can query MSS
    """
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {app_token}"})
    return session


def save_uploaded_file(file: UploadFile, target: Path) -> Path:
    """Saves the uploaded file to the given target path

    Args:
        file: the file to upload
        target: the target path to save to

    Returns:
        the new path to the saved file
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    file.file.seek(0)
    with target.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)
    file.file.close()

    return target


def to_http_error(
    status_code: int, custom_message: Optional[str] = None
) -> Callable[[Request, Exception], Union[Response, Awaitable[Response]]]:
    """An error handler that converts the exception to an HTTPException

    The details in the http error are got from the exception itself.
    It also logs the original error.

    Args:
        status_code: the HTTP status code
        custom_message: a custom message to send to the client

    Returns:
        an HTTP exception handler function
    """

    async def handler(request: Request, exp: Exception) -> Response:
        logging.error(exp)
        message = custom_message
        if message is None:
            message = f"{exp}"

        http_exp = HTTPException(status_code, message)
        return await http_exception_handler(request, http_exp)

    return handler


def encrypt_mss_jwt_token(
    token: str, key_path: Path = settings.MSS_PUBLIC_KEY_PATH
) -> str:
    """Encrypts the token passed so that it is only readable by the right MSS instance

    Args:
        token: the raw token to encrypt
        key_path: the file path to the RSA public key PEM file

    Returns:
        the encrypted token
    """
    mss_pub_key = _get_mss_public_key(key_path=key_path)
    cipher_bytes = mss_pub_key.encrypt(
        token.encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return cipher_bytes.decode()


def _get_mss_public_key(key_path: Path = settings.MSS_PUBLIC_KEY_PATH):
    """Loads the public key for MSS given the path to the key file

    Args:
        key_path: the file path to the RSA public key PEM file

    Returns:
        the public key of the MSS
    """
    global _MSS_PUBLIC_KEY

    if not _MSS_PUBLIC_KEY:
        with open(key_path, "rb") as key_file:
            data = key_file.read()
            _MSS_PUBLIC_KEY = serialization.load_pem_public_key(data)

    return _MSS_PUBLIC_KEY
