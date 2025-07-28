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
import base64
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
from pydantic import BaseModel, ConfigDict, Field
from redis import Redis

import settings

from .datetime import utc_now_str
from .model import IncEx
from .redis_store import Collection, Schema

ITEM = TypeVar("ITEM", bound=BaseModel)

_MSS_PUBLIC_KEY: Optional[RSAPublicKey] = None
_REQUEST_LOGS_STORE: Optional[Collection["RequestLog"]] = None


class RequestLog(Schema):
    """Schema for tracking requests"""

    __primary_key_fields__ = ("request_id",)
    __index_fields__ = (
        "user_id",
        "ip_address",
    )
    model_config = ConfigDict(extra="allow")

    request_id: str
    user_id: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: Optional[str] = Field(default_factory=utc_now_str)
    updated_at: Optional[str] = Field(default_factory=utc_now_str)

    @classmethod
    def from_request(cls, request: Request) -> "RequestLog":
        """Generates a request log object from the request

        Args:
            request: the received HTTP request

        Returns:
            the request log object
        """
        user_id = request.headers.get("x-mss-user-id")
        request_id = request.state.request_id
        return cls(
            request_id=request_id,
            user_id=user_id,
            ip_address=f"{request.client.host}",
        )


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


def get_request_logs_store() -> Collection[RequestLog]:
    """Gets the store for the given url for the request logs

    Returns:
        the RedisCollection containing the jobs
    """
    global _REQUEST_LOGS_STORE
    if _REQUEST_LOGS_STORE is None:
        connection = Redis.from_url(url=settings.RQ_REDIS_URL)
        _REQUEST_LOGS_STORE = Collection(connection=connection, schema=RequestLog)

    return _REQUEST_LOGS_STORE


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


def verify_mss_signature(
    signature: str, message: str, key_path: Path = settings.MSS_PUBLIC_KEY_PATH
) -> None:
    """Verifies that the given message is from MSS, given the signature

    Args:
        signature: the signature of the message signed by MSS
        message: the message from MSS
        key_path: the file path to the RSA public key PEM file

    Raises:
        InvalidSignature: if signature does not match with what would be expected from MSS
    """
    mss_pub_key = _get_mss_public_key(key_path=key_path)
    mss_pub_key.verify(
        base64.b64decode(signature),
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256(),
    )


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
