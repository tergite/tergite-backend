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

"""Websocket connection that is synchronous connecting to MSS"""
from __future__ import annotations

import json
import math
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional, cast

from websockets import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

import settings
from app.utils.logging import err_logger

from .auth import create_headers
from .dtos import DeviceEvent, EventResponse

_MSS_CLIENT: Optional["MssClient"] = None


def get_mss_client() -> "MssClient":
    """Returns a new MssClient."""
    global _MSS_CLIENT
    if _MSS_CLIENT is None:
        _MSS_CLIENT = MssClient(
            uri=str(settings.MSS_DEVICE_EVENTS_ENDPOINT),
            device=settings.DEFAULT_PREFIX,
            private_key_file=settings.PRIVATE_KEY_FILE,
            key_password=settings.PRIVATE_KEY_PASSWORD,
            open_timeout=settings.MSS_CONNECTION_TIMEOUT,
            response_timeout=settings.MSS_RESPONSE_TIMEOUT,
            max_reconnection_delay=settings.MSS_MAX_RECONNECTION_DELAY,
            max_reconnections=settings.MSS_CONNECTION_MAX_ATTEMPTS,
        )

    return _MSS_CLIENT


def disconnect_mss_client(ignore_errors: bool = False):
    """Disconnects the MSSClient, resetting its connections

    Args:
        ignore_errors: Whether to ignore errors when disconnecting. Defaults to False.
    """
    global _MSS_CLIENT
    if isinstance(_MSS_CLIENT, MssClient):
        try:
            _MSS_CLIENT.close()
        except Exception as exp:
            if ignore_errors:
                err_logger.warning(f"error closing MSSClient: {exp}")
            else:
                raise exp

    _MSS_CLIENT = None


class MssClient:
    """A blocking client for making requests to a Main Service Server (MSS) Instance"""

    def __init__(
        self,
        uri: str = str(settings.MSS_DEVICE_EVENTS_ENDPOINT),
        device: str = settings.DEFAULT_PREFIX,
        private_key_file=settings.PRIVATE_KEY_FILE,
        key_password: Optional[bytes] = settings.PRIVATE_KEY_PASSWORD,
        open_timeout: float = settings.MSS_CONNECTION_TIMEOUT,
        response_timeout: float = settings.MSS_RESPONSE_TIMEOUT,
        max_reconnection_delay: float = 60,
        max_reconnections: int | None = None,
        **kwargs: Any,
    ):
        """
        Args:
            uri: the URI to connect to; default = settings.MSS_DEVICE_EVENTS_ENDPOINT
            device: the name of the device; default = settings.DEFAULT_PREFIX
            private_key_file: the path to the private key file; default = settings.PRIVATE_KEY_FILE
            key_password: the password for the private key file; defaults = settings.PRIVATE_KEY_PASSWORD
            open_timeout: the timeout for opening the websocket in seconds; default = settings.MSS_CONNECTION_TIMEOUT
            response_timeout: The timeout for receiving a response from MSS; default = settings.MSS_RESPONSE_TIMEOUT
            max_reconnection_delay: the maximum number of seconds between reconnections; default = 60
            max_reconnections: the maximum number of times to reconnect; default = None, meaning no limit
            kwargs: additional options to pass to websockets.connect
        """
        self._uri = uri
        self._response_timeout = response_timeout
        self._device = device
        self._private_key_file: Path = private_key_file
        self._private_key_password: bytes = key_password
        self._open_timeout = open_timeout
        self._max_reconnection_delay = max_reconnection_delay
        self._device = device
        self._kwargs = kwargs
        self._client: Optional[ClientConnection] = None
        self._max_reconnections = (
            math.inf if max_reconnections is None else max_reconnections
        )
        self.__current_delay: float = 1

    def __connect(self, **kwargs: Any) -> ClientConnection:
        """Connects to MSS returning a new connection

        Args:
            kwargs: additional options to pass to websockets.connect

        Returns:
            The new connection
        """
        auth_headers = create_headers(
            private_key_file=self._private_key_file,
            device=self._device,
            key_password=self._private_key_password,
        )
        options = {**self._kwargs, **kwargs}
        return connect(
            self._uri,
            open_timeout=self._open_timeout,
            additional_headers=auth_headers,
            **options,
        )

    def send(self, payload: str):
        """Sends payload to MSS

        Args:
            payload: the payload to send to MSS
        """
        delay = 1
        start_time = time.time()
        timeout = self._response_timeout
        is_sent = False
        attempts = 0
        max_attempts = self._max_reconnections + 1

        while not is_sent:
            try:
                if time.time() - start_time > timeout:
                    raise TimeoutError(f"response took longer than timeout {timeout}s")

                if attempts >= max_attempts:
                    raise TimeoutError(
                        f"maximum connection attempts {max_attempts} exceeded"
                    )

                if not self._client:
                    attempts += 1
                    self._client = self.__connect()

                self._client.send(payload, text=True)
                is_sent = True
            except (ConnectionClosed, WebSocketException) as e:
                err_logger.warning(
                    f"Connection lost ({e}). Reconnecting in {delay} seconds..."
                )
                self._client = None
                time.sleep(delay)
                delay = min(delay * 2, self._max_reconnection_delay)

    def recv(
        self, filters: dict | None = None, error_prefix: str = ""
    ) -> dict[str, Any]:
        """Receives response from MSS

        Args:
            filters: the filter that the message should match; default = None, meaning the first message to arrive
            error_prefix: Optional prefix to prepend to error messages

        Raises:
            TimeoutError: response took longer than `self._response_timeout`
        """
        delay = 1
        start_time = time.time()
        filters = filters or {}
        timeout = self._response_timeout
        max_attempts = self._max_reconnections + 1
        attempts = 0

        while True:
            try:
                if time.time() - start_time > timeout:
                    raise TimeoutError(
                        f"{error_prefix}response took longer than timeout {timeout}s"
                    )
                if attempts >= max_attempts:
                    raise TimeoutError(
                        f"{error_prefix}maximum connection attempts {max_attempts} exceeded"
                    )

                if not self._client:
                    attempts += 1
                    self._client = self.__connect()

                raw_data = self._client.recv(decode=False, timeout=timeout)
                with suppress(json.JSONDecodeError):
                    response = json.loads(raw_data)
                    if all(response.get(k) == v for k, v in filters.items()):
                        return response

            except (ConnectionClosed, WebSocketException) as e:
                err_logger.warning(
                    f"Connection lost ({e}). Reconnecting in {delay} seconds..."
                )
                self._client = None
                time.sleep(delay)
                delay = min(delay * 2, self._max_reconnection_delay)

    def send_event(self, payload: DeviceEvent, error_prefix: str = "") -> EventResponse:
        """Sends an event payload to MSS

        It does an exponential backoff reconnection in case of a disconnection

        Args:
            payload: the payload to send to MSS
            error_prefix: Optional prefix to prepend to error messages

        Returns:
            the event response

        Raises:
            ValueError: {error_prefix}{error message}
            TimeoutError: {error_prefix}response took longer than timeout
            RuntimeError: {error_prefix}no connection yet
        """
        event_id = payload.id
        event_json = payload.model_dump_json()
        self.send(event_json)
        resp = self.recv(filters={"id": event_id}, error_prefix=error_prefix)
        if resp.get("status") != "success":
            raise ValueError(f"{error_prefix}{resp.get('detail')}")
        return cast(EventResponse, resp)

    def __enter__(self):
        """Creates a context manager that yields MSSClient instance"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exits from the context manager"""
        self._client.close()

    def close(self) -> None:
        """Closes client to MSS"""
        if self._client:
            self._client.close()
