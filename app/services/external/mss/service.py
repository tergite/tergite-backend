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
import abc
import asyncio
import base64
import json
import logging
import time
import uuid
from abc import ABC
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, Optional

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import PubSub as AsyncPubSub
from redis.client import PubSub
from websockets import ClientConnection

import settings
from app.utils.redis import get_redis_connection

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


class BaseMssClientPipe(ABC):
    """The pipe for sending messages and receiving messages from MSS"""

    def __init__(
        self,
        device: str = settings.DEFAULT_PREFIX,
        timeout: float = 120,
        redis_url: str = settings.RQ_REDIS_URL,
        **kwargs,
    ):
        """
        Args:
            device: The name of this device
            redis_url: The URL to the redis connection where the PubSub is
            timeout: The timeout for receiving a response from the pipe
            is_async: Whether the pipe is asynchronous or not
        """
        self._device = device
        self._outbox_name: str = get_outbox_channel_name(device)
        self._inbox_name: str = get_inbox_channel_name(device)
        self._timeout: float = timeout
        self._redis_url: str = redis_url

    @abc.abstractmethod
    def send_event(self, payload: DeviceEvent, error_prefix: str = "") -> EventResponse:
        """Sends an event payload to MSS

        Call this only in synchronous code

        Args:
            payload: the payload to send to MSS
            error_prefix: the prefix to append to the error message

        Returns:
            the event response got from MSS

        Raises:
            ValueError: {error_prefix}{error message}
            TimeoutError: {error_prefix}response took longer than timeout
            RuntimeError: loop is already running
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Close the pipe connection to the MSS client"""


class MssClientPipe(BaseMssClientPipe):
    """Pipe to MSS client that is synchronous, to be used on the RQ side mainly"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._redis: Redis = get_redis_connection(self._redis_url)
        self._inbox: PubSub = self._redis.pubsub(ignore_subscribe_messages=True)

    def send_event(self, payload: DeviceEvent, error_prefix: str = "") -> EventResponse:
        """Sends an event payload to MSS

        Args:
            payload: the payload to send to MSS
            error_prefix: Optional prefix to prepend to error messages

        Returns:
            the event response

        Raises:
            ValueError: {error_prefix}{error message}
            TimeoutError: {error_prefix}response took longer than timeout
        """
        event_id = payload.id
        event_json = payload.model_dump_json()
        self._inbox.subscribe(self._inbox_name)
        self._redis.publish(self._outbox_name, event_json)

        start_time = time.time()
        while True:
            if time.time() - start_time > self._timeout:
                raise TimeoutError(
                    f"{error_prefix}response took longer than timeout {self._timeout}s"
                )

            channel_resp = self._inbox.get_message(timeout=self._timeout)
            if channel_resp is None:
                continue

            response = json.loads(channel_resp["data"])  # type: EventResponse
            # ignore responses that are not for this event
            if response["id"] != event_id:
                continue

            if response["status"] != "success":
                raise ValueError(f"{error_prefix}{response['detail']}")

            return response

    def close(self) -> None:
        """Close the pipe connection to the MSS client"""
        self._inbox.unsubscribe()
        self._inbox.close()
        self._redis.close()

    def __enter__(self) -> "MssClientPipe":
        self._inbox.subscribe(self._inbox_name)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class AsyncMssClientPipe(BaseMssClientPipe):
    """Pipe to MSS client that is asynchronous, to be used on the FastAPI side"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._redis: AsyncRedis = get_redis_connection(self._redis_url, is_async=True)
        self._inbox: AsyncPubSub = self._redis.pubsub(ignore_subscribe_messages=True)

    async def send_event(
        self, payload: DeviceEvent, error_prefix: str = ""
    ) -> EventResponse:
        """Sends an event payload to MSS asynchronously

        Args:
            payload: the payload to send to MSS
            error_prefix: Optional prefix to prepend to error messages

        Returns:
            the event response

        Raises:
            ValueError: {error_prefix}{error message}
            TimeoutError: {error_prefix}response took longer than timeout
        """
        event_id = payload.id
        event_json = payload.model_dump_json()
        await self._inbox.subscribe(self._inbox_name)
        await self._redis.publish(self._outbox_name, event_json)

        start_time = time.time()
        while True:
            # to allow other tasks to run, we wait
            await asyncio.sleep(0.01)

            if time.time() - start_time > self._timeout:
                raise TimeoutError(
                    f"{error_prefix}response took longer than timeout {self._timeout}s"
                )

            channel_resp = await self._inbox.get_message(timeout=self._timeout)
            if channel_resp is None:
                continue

            response = json.loads(channel_resp["data"])  # type: EventResponse
            # ignore responses that are not for this event
            if response["id"] != event_id:
                continue

            if response["status"] != "success":
                raise ValueError(f"{error_prefix}{response['detail']}")

            return response

    async def close(self) -> None:
        """Close the pipe connection to the MSS client asynchronously"""
        await self._inbox.unsubscribe()
        await self._inbox.aclose()
        await self._redis.aclose()

    async def __aenter__(self) -> "AsyncMssClientPipe":
        await self._inbox.subscribe(self._inbox_name)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


class AsyncMssClient(websockets.connect):
    """An asynchronous client for making requests to a Main Service Server (MSS) Instance in the background"""

    def __init__(
        self,
        uri: str = str(settings.MSS_DEVICE_EVENTS_ENDPOINT),
        device: str = settings.DEFAULT_PREFIX,
        private_key_file=settings.PRIVATE_KEY_FILE,
        key_password: Optional[bytes] = settings.PRIVATE_KEY_PASSWORD,
        open_timeout: float = settings.MSS_CONNECTION_TIMEOUT,
        redis_url: str = settings.RQ_REDIS_URL,
        **kwargs: Any,
    ):
        """
        Args:
            uri: the URI to connect to; default = settings.MSS_DEVICE_EVENTS_ENDPOINT
            device: the name of the device; default = settings.DEFAULT_PREFIX
            private_key_file: the path to the private key file; default = settings.PRIVATE_KEY_FILE
            key_password: the password for the private key file; defaults = settings.PRIVATE_KEY_PASSWORD
            open_timeout: the timeout for opening the websocket in seconds; default = settings.MSS_CONNECTION_TIMEOUT
            redis_url: the redis URL connection to use for PubSub; default = settings.RQ_REDIS_URL
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
        self._redis: AsyncRedis = get_redis_connection(url=redis_url, is_async=True)
        self.outbox: AsyncPubSub = self._redis.pubsub(ignore_subscribe_messages=True)

    async def _outbox_handler(self, msg: dict) -> None:
        """Handles messages sent to the outbox

        Args:
            msg: the message to process
        """
        await self.connection.send(msg["data"], text=True)
        response_str = await self.connection.recv(decode=False)
        await self._redis.publish(self._inbox_pubsub, response_str)

    async def __aenter__(self) -> ClientConnection:
        await self.outbox.subscribe(**{self._outbox_pubsub: self._outbox_handler})
        loop = asyncio.get_running_loop()

        self.__outbox_task = loop.create_task(
            self.outbox.run(exception_handler=_pubsub_exception_handler)
        )
        return await super().__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.outbox.unsubscribe()
        self.__outbox_task.cancel()
        await super().__aexit__(exc_type, exc_value, traceback)


async def _pubsub_exception_handler(e: BaseException, pubsub: AsyncPubSub):
    """Handles exceptions raised by PubSub

    Args:
        e: the exception raised
        pubsub: the PubSub instance
    """
    logging.error(e)
    await pubsub.unsubscribe()
    raise e


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
