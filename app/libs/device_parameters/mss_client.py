# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2025, 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Utility for the client that connects to MSS server"""
import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
import websocket
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

import settings
from app.utils.api import GeneralMessage

from .dtos import DeviceEvent

_BCC_PRIVATE_KEYS: Dict[str, RSAPrivateKey] = {}
_DEFAULT_MSS_CLIENT: Optional["MssClient"] = None


def get_default_mss_client() -> "MssClient":
    """Gets the default MSS client as derived from settings"""
    global _DEFAULT_MSS_CLIENT
    if _DEFAULT_MSS_CLIENT is None:
        _DEFAULT_MSS_CLIENT = MssClient()
    elif not _DEFAULT_MSS_CLIENT.is_connected:
        _DEFAULT_MSS_CLIENT.reconnect()

    return _DEFAULT_MSS_CLIENT


class MssClient:
    """A client for making requests to a Main Service Server (MSS) Instance"""

    def __init__(
        self,
        uri: str = str(settings.MSS_DEVICE_EVENTS_ENDPOINT),
        device: str = settings.DEFAULT_PREFIX,
        private_key_file=settings.PRIVATE_KEY_FILE,
        key_password: Optional[bytes] = settings.PRIVATE_KEY_PASSWORD,
        timeout: float = settings.MSS_CONNECTION_TIMEOUT,
        **options: Any,
    ):
        """
        Args:
            uri: the URI to connect to; default = settings.MSS_DEVICE_EVENTS_ENDPOINT
            device: the name of the device; default = settings.DEFAULT_PREFIX
            private_key_file: the path to the private key file; default = settings.PRIVATE_KEY_FILE
            key_password: the password for the private key file; defaults = settings.PRIVATE_KEY_PASSWORD
            timeout: the socket timeout in seconds; default = settings.MSS_CONNECTION_TIMEOUT
            options: additional options to pass to websocket.create_connection
        """
        auth_headers = _create_headers(
            private_key_file=private_key_file,
            device=device,
            key_password=key_password,
        )
        header = [*options.pop("header", []), *auth_headers]
        self.__connection_args: Dict[str, Any] = {
            "url": uri,
            "header": header,
            "timeout": timeout,
            **options,
        }
        self._ws: websocket.WebSocket = websocket.create_connection(
            **self.__connection_args
        )

    @property
    def is_connected(self) -> bool:
        """If the client is currently connected"""
        return self._ws.connected

    def reconnect(self) -> None:
        """Reconnect the client to the Main Service Server"""
        if self._ws.connected:
            self._ws.shutdown()
        self._ws = websocket.create_connection(**self.__connection_args)

    def send_event(
        self, payload: DeviceEvent, error_prefix: str = ""
    ) -> GeneralMessage:
        """Sends an event payload to MSS

        Args:
            payload: the payload to send to MSS
            error_prefix: the prefix to append to the error message

        Returns:
            the general message got from MSS

        Raises:
            ValueError: {error_prefix}{error message}
        """
        self._ws.send(payload.model_dump_json())
        response_str = self._ws.recv()
        response = json.loads(response_str)
        if response["status"] != "success":
            raise ValueError(f"{error_prefix}{response['detail']}")

        return response

    def close(self):
        """Closes the websocket connection"""
        try:
            self._ws.shutdown()
        except websocket.WebSocketConnectionClosedException:
            pass

    def __enter__(self):
        if not self.is_connected:
            self.reconnect()
        return self

    def __exit__(self, *args):
        self.close()


def _create_headers(
    private_key_file: Path,
    device: str = "",
    key_password: Optional[bytes] = None,
) -> list[str]:
    """Creates headers to show that the request is a valid one from BCC

    Args:
        private_key_file: the path to the private key file
        device: the name of this device
        key_password: the password used to encrypt the key PEM file

    Returns:
        The list of headers that show a given request is from BCC
    """
    request_id = str(uuid.uuid4())
    timestamp = time.time()
    message = f"{device}-{request_id}-{timestamp}"
    signature = _sign_message(private_key_file, message=message, password=key_password)
    return [
        f"x-request-id: {request_id}",
        f"x-timestamp: {timestamp}",
        f"x-signature: {signature}",
        f"x-id: {device}",
    ]


def _sign_message(key_file: Path, message: str, password: Optional[bytes]) -> str:
    """Creates an BCC-signed signature given a message

    Args:
        key_file: the path to the private RSA key
        message: the message
        password: the password used to encrypt the key PEM file

    Returns:
        the string form of the signature
    """
    mss_private_key = _get_private_key(key_file, password=password)
    signature = mss_private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def _get_private_key(key_file: Path, password: Optional[bytes]) -> RSAPrivateKey:
    """Loads the private key for BCC

    Args:
        key_file: the path to the private key file
        password: the password that the private key was encrypted with

    Returns:
        the private key of the BCC
    """
    global _BCC_PRIVATE_KEYS

    key_file_str = str(key_file)

    try:
        return _BCC_PRIVATE_KEYS[key_file_str]
    except KeyError:
        with open(key_file, "rb") as file:
            key = _BCC_PRIVATE_KEYS[key_file_str] = serialization.load_pem_private_key(
                file.read(), password=password
            )
        return key
