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
import json
import math
import time
from contextlib import suppress
from typing import Any, Callable, Optional, Self, TypeVar, cast

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from websockets import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

from utils.config import DeviceSettings
from utils.exc import MssRejectedError, MssTimeoutError
from utils.logging import err_logger

from .auth import create_headers
from .dtos import DeviceEvent, EventResponse

_T = TypeVar("_T")


class MssClient:
    """A blocking client for making requests to a Main Service Server (MSS) Instance"""

    def __init__(
        self,
        uri: str,
        device: str,
        private_key: RSAPrivateKey,
        open_timeout: float = 5,
        response_timeout: float = 120,
        max_reconnection_delay: float = 60,
        max_reconnections: int | None = None,
        **kwargs: Any,
    ):
        """
        Args:
            uri: the URI to connect to
            device: the name of the device
            private_key: the private key of this BCC, which signs its connection
            open_timeout: the timeout for opening the websocket in seconds; default = 5
            response_timeout: The timeout for receiving a response from MSS; default = 120
            max_reconnection_delay: the maximum number of seconds between reconnections; default = 60
            max_reconnections: the maximum number of times to reconnect; default = None, meaning no limit
            kwargs: additional options to pass to websockets.connect
        """
        self._uri = uri
        self._response_timeout = response_timeout
        self._device = device
        self._private_key = private_key
        self._open_timeout = open_timeout
        self._max_reconnection_delay = max_reconnection_delay
        self._kwargs = kwargs
        self._client: Optional[ClientConnection] = None
        self._max_reconnections = (
            math.inf if max_reconnections is None else max_reconnections
        )

    @classmethod
    def from_settings(
        cls, device_settings: DeviceSettings, private_key: RSAPrivateKey
    ) -> Self:
        """Constructs the client of the device of the given settings, which signs its
        connection with the given private key"""
        return cls(
            uri=f"{device_settings.mss_device_events_endpoint}",
            device=device_settings.default_prefix,
            private_key=private_key,
            open_timeout=device_settings.mss_connection_timeout,
            response_timeout=device_settings.mss_response_timeout,
            max_reconnection_delay=device_settings.mss_max_reconnection_delay,
            max_reconnections=device_settings.mss_connection_max_attempts,
        )

    def __connect(self, **kwargs: Any) -> ClientConnection:
        """Connects to MSS returning a new connection

        Args:
            kwargs: additional options to pass to websockets.connect

        Returns:
            The new connection
        """
        auth_headers = create_headers(
            private_key=self._private_key, device=self._device
        )
        options = self._kwargs | kwargs
        return connect(
            self._uri,
            open_timeout=self._open_timeout,
            additional_headers=auth_headers,
            **options,
        )

    def send_event(self, payload: DeviceEvent, error_prefix: str = "") -> EventResponse:
        """Sends an event payload to MSS, and waits for MSS's reply to it

        A connection lost before the reply is reopened, after a growing delay, and the
        event sent again on it, as the reply to it on the old one is lost with it

        Args:
            payload: the payload to send to MSS
            error_prefix: Optional prefix to prepend to error messages

        Returns:
            the event response

        Raises:
            MssRejectedError: {error_prefix}{error message}
            MssTimeoutError: {error_prefix}response took longer than timeout
            RuntimeError: {error_prefix}no connection yet
        """
        event_json = payload.model_dump_json()
        deadline = time.monotonic() + self._response_timeout

        def send_and_receive_reply(client: ClientConnection) -> dict[str, Any]:
            client.send(event_json, text=True)
            return _receive_reply(client, event_id=payload.id, deadline=deadline)

        try:
            resp = self._run_connected(send_and_receive_reply, deadline=deadline)
        except TimeoutError as exp:
            raise MssTimeoutError(f"{error_prefix}{exp}")
        if resp.get("status") != "success":
            raise MssRejectedError(f"{error_prefix}{resp.get('detail')}")
        return cast(EventResponse, resp)

    def _run_connected(
        self,
        operation: Callable[[ClientConnection], _T],
        deadline: Optional[float] = None,
    ) -> _T:
        """Calls the operation on the connection to MSS, connecting first, and reconnecting
        after a growing delay while the connection fails, until the deadline, by default
        the response timeout from now, or the most connection attempts

        Raises:
            TimeoutError: response took longer than timeout, or maximum connection attempts exceeded
        """
        if deadline is None:
            deadline = time.monotonic() + self._response_timeout
        delay = 1
        attempts = 0
        max_attempts = self._max_reconnections + 1
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"response took longer than timeout {self._response_timeout}s"
                )
            if attempts >= max_attempts:
                raise TimeoutError(
                    f"maximum connection attempts {max_attempts} exceeded"
                )
            try:
                if not self._client:
                    attempts += 1
                    self._client = self.__connect()
                return operation(self._client)
            except TimeoutError:
                raise
            except (ConnectionClosed, WebSocketException, OSError) as e:
                err_logger.warning(
                    f"Connection lost ({e}). Reconnecting in {delay} seconds..."
                )
                self._client = None
                time.sleep(delay)
                delay = min(delay * 2, self._max_reconnection_delay)

    def close(self) -> None:
        """Closes client to MSS"""
        if self._client:
            self._client.close()


def _receive_reply(
    client: ClientConnection, event_id: str, deadline: float
) -> dict[str, Any]:
    """Receives MSS's reply to the event, waiting what is left to the deadline, and
    skipping the other messages

    Raises:
        TimeoutError: the deadline passed
    """
    while True:
        seconds_left = max(deadline - time.monotonic(), 0)
        raw_data = client.recv(decode=False, timeout=seconds_left)
        with suppress(json.JSONDecodeError):
            response = json.loads(raw_data)
            if response.get("id") == event_id:
                return response
