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
import shutil
from pathlib import Path
from typing import (
    Any,
    Generic,
    List,
    Literal,
    NotRequired,
    Optional,
    TypedDict,
    TypeVar,
)

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import UploadFile
from pydantic import BaseModel

import settings

from .model import IncEx

ITEM = TypeVar("ITEM", bound=BaseModel)

_MSS_PUBLIC_KEYS: dict[Path, RSAPublicKey] = {}


class CancellationDetails(BaseModel):
    """Details to do with a given cancellation request"""

    reason: Optional[str] = None


class GeneralMessage(TypedDict):
    """A general message object sent on the API"""

    status: Literal["success", "error", "cancelled", "failed"]
    detail: NotRequired[str]
    data: NotRequired[dict | list]


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


def encrypt_mss_jwt_token(token: str, key_path: Optional[Path] = None) -> str:
    """Encrypts the token passed so that it is only readable by the right MSS instance

    Args:
        token: the raw token to encrypt
        key_path: the file path to the RSA public key PEM file; default = settings.MSS_PUBLIC_KEY_PATH

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
    return base64.b64encode(cipher_bytes).decode()


def verify_mss_signature(
    signature: str, message: str, key_path: Optional[Path] = None
) -> None:
    """Verifies that the given message is from MSS, given the signature

    Args:
        signature: the signature of the message signed by MSS
        message: the message from MSS
        key_path: the file path to the RSA public key PEM file; default = settings.MSS_PUBLIC_KEY_PATH

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


def _get_mss_public_key(key_path: Optional[Path] = None):
    """Loads the public key for MSS given the path to the key file

    Args:
        key_path: the file path to the RSA public key PEM file; default = settings.MSS_PUBLIC_KEY_PATH

    Returns:
        the public key of the MSS
    """
    key_path = key_path or settings.MSS_PUBLIC_KEY_PATH
    mss_public_key = _MSS_PUBLIC_KEYS.get(key_path)
    if mss_public_key is None:
        with open(key_path, "rb") as key_file:
            mss_public_key = serialization.load_pem_public_key(key_file.read())
        _MSS_PUBLIC_KEYS[key_path] = mss_public_key

    return mss_public_key
