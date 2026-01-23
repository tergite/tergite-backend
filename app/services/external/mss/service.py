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
import logging
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, Optional

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from redis import Redis
from redis.client import PubSub, PubSubWorkerThread
from websockets import ClientConnection

import settings

from .dtos import DeviceEvent, EventResponse

_BCC_PRIVATE_KEYS: Dict[str, RSAPrivateKey] = {}
_MSS_CLIENT_PIPE: Optional["MssClientPipe"] = None


def get_inbox_channel_name(device: str = settings.DEFAULT_PREFIX) -> str:
    """Get the name of the inbox channel for the MSS client

    This is where we see messages received from the MSS server

    Args:
        device: The name of this device

    Returns:
        the name of the inbox channel
    """
    return f"{device}:mss:inbox"


def get_outbox_channel_name(device: str = settings.DEFAULT_PREFIX) -> str:
    """Get the name of the outbox channel for the MSS client

    This is where we send messages to be sent to the MSS server

    Args:
        device: The name of this device

    Returns:
        the name of the inbox channel
    """
    return f"{device}:mss:outbox"


def get_default_mss_client_pipe() -> "MssClientPipe":
    """Gets the default MSS client pipe"""
    global _MSS_CLIENT_PIPE

    if _MSS_CLIENT_PIPE is None:
        _MSS_CLIENT_PIPE = MssClientPipe()
    return _MSS_CLIENT_PIPE


class MssClientPipe:
    """The pipe for sending messages and receiving messages from MSS"""

    def __init__(
        self,
        device: str = settings.DEFAULT_PREFIX,
        redis_connection: Redis = settings.REDIS_CONNECTION,
        timeout: float = 120,
    ):
        """
        Args:
            device: The name of this device
            redis_connection: The redis connection where the PubSub is
            timeout: The timeout for receiving a response from the pipe
        """
        self._device = device
        self._outbox_name: str = get_outbox_channel_name(device)
        self._inbox_name: str = get_inbox_channel_name(device)
        self._redis: Redis = redis_connection
        self._timeout: float = timeout

        self.inbox: PubSub = self._redis.pubsub(ignore_subscribe_messages=True)
        self.inbox.subscribe(self._inbox_name)

    def send_event(self, payload: DeviceEvent, error_prefix: str = "") -> EventResponse:
        """Sends an event payload to MSS

        Args:
            payload: the payload to send to MSS
            error_prefix: the prefix to append to the error message

        Returns:
            the event response got from MSS

        Raises:
            ValueError: {error_prefix}{error message}
            TimeoutError: {error_prefix}response took longer than timeout
        """
        event_id = payload.id
        self._redis.publish(self._outbox_name, payload.model_dump_json())
        start_time = time.time()

        while True:
            if time.time() - start_time > self._timeout:
                raise TimeoutError(
                    f"{error_prefix}response took longer than timeout {self._timeout}s"
                )

            response_str = self.inbox.get_message(timeout=self._timeout)
            response = json.loads(response_str)  # type: EventResponse
            # ignore responses that are not for this event
            if response["id"] != event_id:
                continue

            if response["status"] != "success":
                raise ValueError(f"{error_prefix}{response['detail']}")

        return response


class AsyncMssClient(websockets.connect):
    """An asynchronous client for making requests to a Main Service Server (MSS) Instance in the background"""

    def __init__(
        self,
        uri: str = str(settings.MSS_DEVICE_EVENTS_ENDPOINT),
        device: str = settings.DEFAULT_PREFIX,
        private_key_file=settings.PRIVATE_KEY_FILE,
        key_password: Optional[bytes] = settings.PRIVATE_KEY_PASSWORD,
        open_timeout: float = settings.MSS_CONNECTION_TIMEOUT,
        redis_connection: Redis = settings.REDIS_CONNECTION,
        **kwargs: Any,
    ):
        """
        Args:
            uri: the URI to connect to; default = settings.MSS_DEVICE_EVENTS_ENDPOINT
            device: the name of the device; default = settings.DEFAULT_PREFIX
            private_key_file: the path to the private key file; default = settings.PRIVATE_KEY_FILE
            key_password: the password for the private key file; defaults = settings.PRIVATE_KEY_PASSWORD
            open_timeout: the timeout for opening the websocket in seconds; default = settings.MSS_CONNECTION_TIMEOUT
            redis_connection: the redis connection to use for PubSub; default = settings.REDIS_CONNECTION
            kwargs: additional options to pass to websockets.connect
        """
        auth_headers = _create_headers(
            private_key_file=private_key_file,
            device=device,
            key_password=key_password,
        )
        kwargs["additional_headers"] = {
            **kwargs.pop("additional_headers", {}),
            **auth_headers,
        }
        super().__init__(uri, open_timeout=open_timeout, **kwargs)

        self.__uri = uri

        self._outbox_pubsub: str = get_outbox_channel_name(device)
        self._inbox_pubsub: str = get_inbox_channel_name(device)
        self._redis: Redis = redis_connection
        self.outbox = self._redis.pubsub(ignore_subscribe_messages=True)

    async def __aenter__(self) -> ClientConnection:
        def outbox_handler(msg: dict):
            self.connection.send(msg["data"])
            response_str = self.connection.recv()
            self._redis.publish(self._inbox_pubsub, response_str)

        def outbox_exception_handler(
            exp: Exception, pubsub: PubSub, thread: PubSubWorkerThread
        ):
            logging.error(exp)
            pubsub.unsubscribe()
            thread.stop()
            raise exp

        self.outbox.subscribe(**{self._outbox_pubsub: outbox_handler})
        self.__outbox_thread = self.outbox.run_in_thread(
            sleep_time=0.001, exception_handler=outbox_exception_handler
        )
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.outbox.unsubscribe()
        self.__outbox_thread.stop()
        await super().__aexit__(exc_type, exc_value, traceback)


def _create_headers(
    private_key_file: Path,
    device: str = "",
    key_password: Optional[bytes] = None,
) -> dict[str, str]:
    """Creates headers to show that the request is a valid one from BCC

    Args:
        private_key_file: the path to the private key file
        device: the name of this device
        key_password: the password used to encrypt the key PEM file

    Returns:
        The dict of headers that show a given request is from BCC
    """
    request_id = str(uuid.uuid4())
    timestamp = time.time()
    message = f"{device}-{request_id}-{timestamp}"
    signature = _sign_message(private_key_file, message=message, password=key_password)
    return {
        "x-request-id": request_id,
        "x-timestamp": f"{timestamp}",
        "x-signature": signature,
        "x-id": device,
    }


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
