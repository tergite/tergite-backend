# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Utilities for the api"""
import base64
import time
from typing import Dict, List, Optional
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.tests.utils.env import TEST_MSS_PRIVATE_KEY_PATH


def create_invalid_mss_headers(user_id: str = "") -> List[Dict[str, str]]:
    """Creates MSS headers that would be invalid

    Args:
        user_id: the unique identifier of the user

    Returns:
        The dictionary of headers that are invalid
    """
    request_id = f"{uuid4()}"
    timestamp = time.time()

    return [
        {
            "x-mss-request-id": request_id,
            "x-mss-timestamp": f"{timestamp}",
            "x-mss-signature": f"{create_mss_signature(f"{request_id}-{request_id}-{timestamp}")}",
            "x-mss-user-id": user_id,
        },
        {
            "x-mss-request-id": request_id,
            "x-mss-timestamp": f"{timestamp}",
            "x-mss-signature": f"{create_mss_signature(f"{request_id}-{request_id}-{timestamp}")}",
            "x-mss-user-id": user_id,
        },
        {
            "x-mss-request-id": request_id,
            "x-mss-timestamp": f"{timestamp - 500000}",
            "x-mss-signature": f"{create_mss_signature(f"{request_id}-{request_id}-{timestamp}")}",
            "x-mss-user-id": user_id,
        },
        {
            "x-mss-request-id": request_id,
            "x-mss-timestamp": f"{timestamp}",
            "x-mss-signature": f"{create_mss_signature(f"{user_id}-{timestamp}")}",
            "x-mss-user-id": user_id,
        },
        {
            "x-mss-request-id": request_id,
            "x-mss-signature": f"{create_mss_signature(f"{user_id}-{request_id}")}",
            "x-mss-user-id": user_id,
        },
        {},
        {
            "x-mss-request-id": request_id,
            "x-mss-user-id": user_id,
            "x-mss-timestamp": f"{timestamp}",
        },
    ]


def create_mss_headers(
    user_id: str = "", is_admin: Optional[bool] = None
) -> Dict[str, str]:
    """Creates headers to show that the request is a valid one from MSS

    Args:
        user_id: the unique identifier of the user
        is_admin: whether the request should show that the user is an admin

    Returns:
        The dictionary of headers that show a given request is from MSS
    """
    request_id = f"{uuid4()}"
    timestamp = time.time()
    message = f"{user_id}-{request_id}-{timestamp}"
    signature = create_mss_signature(message)
    headers = {
        "x-mss-request-id": request_id,
        "x-mss-timestamp": f"{timestamp}",
        "x-mss-signature": signature,
        "x-mss-user-id": user_id,
    }
    if is_admin is not None:
        headers["x-mss-is-admin"] = f"{is_admin}"

    return headers


def create_mss_signature(message: str) -> str:
    """Creates an MSS-signed signature given a message

    Args:
        message: the message from MSS

    Returns:
        the string form of the signature
    """
    mss_private_key = _get_mss_private_key()
    signature = mss_private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def _get_mss_private_key() -> RSAPrivateKey:
    """Loads the private key for MSS

    Returns:
        the private key of the MSS
    """
    with open(TEST_MSS_PRIVATE_KEY_PATH, "rb") as file:
        return serialization.load_pem_private_key(file.read(), password=None)
