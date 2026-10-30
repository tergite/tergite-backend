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
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    TypedDict,
)
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from tests.utils.env import TEST_MSS_PRIVATE_KEY_PATH

# marks headers for sign_mss_request to sign, once it knows the method and path
_SHOULD_SIGN_HEADER = "x-test-should-sign-mss"
_MSS_PRIVATE_KEY: Optional[RSAPrivateKey] = None


def get_test_client(
    app: FastAPI, should_raise_server_exceptions: bool = True
) -> TestClient:
    """Gets a test client of the app that signs the MSS headers create_mss_headers makes"""
    client = TestClient(app, raise_server_exceptions=should_raise_server_exceptions)
    client.event_hooks = {"request": [sign_mss_request]}
    return client


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
    user_id: str = "",
    is_admin: Optional[bool] = None,
    timestamp: Optional[float] = None,
) -> Dict[str, str]:
    """Creates headers to show that the request is a valid one from MSS

    A client from get_test_client signs them as it sends the request, as the signature
    covers its method and path.

    Args:
        user_id: the unique identifier of the user
        is_admin: whether the request should show that the user is an admin
        timestamp: when MSS signed them; default = now

    Returns:
        The dictionary of headers that show a given request is from MSS
    """
    if timestamp is None:
        timestamp = time.time()
    headers = {
        "x-mss-request-id": f"{uuid4()}",
        "x-mss-timestamp": f"{timestamp}",
        "x-mss-user-id": user_id,
        _SHOULD_SIGN_HEADER: "true",
    }
    if is_admin is not None:
        headers["x-mss-is-admin"] = f"{is_admin}"

    return headers


def sign_mss_request(request: httpx.Request) -> None:
    """Signs the MSS headers of the request, if create_mss_headers made them"""
    if request.headers.pop(_SHOULD_SIGN_HEADER, None) is None:
        return

    headers = request.headers
    message = (
        f"{headers['x-mss-user-id']}|{headers['x-mss-request-id']}"
        f"|{headers['x-mss-timestamp']}|{headers.get('x-mss-is-admin', '')}"
        f"|{request.method}|{request.url.path}"
    )
    request.headers["x-mss-signature"] = create_mss_signature(message)


def decrypt_access_token(access_token: str) -> str:
    """Decrypts the access token the BCC encrypted for MSS, as MSS does

    Args:
        access_token: the access_token of a /token response

    Returns:
        the job JWT
    """
    cipher_bytes = base64.b64decode(access_token)
    token_bytes = _get_mss_private_key().decrypt(
        cipher_bytes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return token_bytes.decode()


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


def get_bearer_headers(token: str) -> Dict[str, Any]:
    """Gets the headers corresponding to the given token

    Args:
        token: the token of the user

    Returns:
        the dict of headers
    """
    return {"Authorization": f"Bearer {token}"}


def get_token(client: "TestClient", data: "LoginDetails") -> Tuple[str, "Response"]:
    """Mimics logging in via MSS and returns the user's JWT token and the httpx.Response

    Args:
        client: the fastapi TestClient for testing the app
        data: the info for signing in

    Returns:
        the tuple of the JWT of the user and the httpx.Response
    """
    headers = create_mss_headers(user_id=data["user_id"])
    response = client.post("/token", json=data, headers=headers)
    token = decrypt_access_token(response.json()["access_token"])
    return token, response


class LoginDetails(TypedDict):
    """Details used to log in"""

    user_id: str
    job_id: str


def get_status_codes_as_mss_admin(
    client: "TestClient", routes: List[Tuple[str, str]]
) -> Dict[str, int]:
    """Gets the status code of each route, as "<method> <path>", requested as an MSS admin"""
    return {
        f"{method} {path}": client.request(
            method, path, headers=create_mss_headers(is_admin=True)
        ).status_code
        for method, path in routes
    }


def _get_mss_private_key() -> RSAPrivateKey:
    """Loads the private key for MSS

    Returns:
        the private key of the MSS
    """
    global _MSS_PRIVATE_KEY

    if not isinstance(_MSS_PRIVATE_KEY, RSAPrivateKey):
        with open(TEST_MSS_PRIVATE_KEY_PATH, "rb") as file:
            _MSS_PRIVATE_KEY = serialization.load_pem_private_key(
                file.read(), password=None
            )

    return _MSS_PRIVATE_KEY
