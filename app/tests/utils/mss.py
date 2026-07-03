# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2024
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Utilities for stuff related to MSS connection"""
import json
import logging
import re
import threading
from collections.abc import Iterable
from datetime import datetime
from typing import Dict, List, Optional, Union

import websockets
from cryptography.exceptions import InvalidSignature
from websockets import ClientProtocol, HeadersLike
from websockets.frames import CloseCode
from websockets.http11 import USER_AGENT
from websockets.sync.client import ClientConnection
from websockets.typing import DataLike
from websockets.uri import parse_uri

_MESSAGE_TYPES = (
    "initialized",
    "recalibrated",
    "job_updated",
    "switched_on",
    "switched_off",
)

_WS_PATH_PATTERN = re.compile(r"/devices/ws/(?P<name>[a-zA-Z0-9_-]+)")
type Data = Union[bytes, str]

_default_protocol = ClientProtocol(uri=parse_uri("ws://localhost:8000"))


class MockWebsocket(ClientConnection):
    """Sync mock of websockets.sync.client.ClientConnection for testing MSS."""

    def __init__(self, sock=None, protocol=_default_protocol, *args, **kwargs):
        # Skip super().__init__() — it starts a recv_events thread that
        # performs real socket I/O; our mock overrides all I/O methods.
        self.__message_types = _MESSAGE_TYPES
        self.__url: Optional[str] = None
        self.__conn_kwargs: Dict[str, str] = {}
        self.__outbox: List[Union[bytes, str]] = []
        self.__inbox: List[str] = []
        self.__pings: List[datetime] = []
        self._connected = False

    def handshake(
        self,
        additional_headers: HeadersLike | None = None,
        user_agent_header: str | None = USER_AGENT,
        timeout: float | None = None,
    ) -> None:
        """Handshake the websocket"""
        if self._connected:
            raise RuntimeError("Connection already established")

        _verify_headers(additional_headers)

        self.__outbox = []
        self.__inbox = []
        self.__pings = []
        self._connected = True

    def ping(
        self,
        data: DataLike | None = None,
        ack_on_close: bool = False,
    ) -> threading.Event:
        """Pretends to send a ping; returns a pre-set Event (pong already received)."""
        self.__pings.append(datetime.now())
        event = threading.Event()
        event.set()
        return event

    def send(
        self,
        message: DataLike | Iterable[DataLike],
        text: bool | None = None,
    ) -> None:
        """Send a payload and enqueue the mock response."""
        if not self._connected:
            raise websockets.ConnectionClosed(
                rcvd=websockets.Close(code=1008, reason="socket is already closed."),
                sent=None,
            )

        if not text:
            raise websockets.ConnectionClosed(
                rcvd=websockets.Close(
                    code=1008,
                    reason="invalid data type: websocket only supports text data",
                ),
                sent=None,
            )

        self.__outbox.append(message)
        event_id: str = "unknown"
        try:
            payload_json = json.loads(message)
            event_id = payload_json["id"]
            if payload_json["name"] not in self.__message_types:
                raise ValueError(f"'{payload_json['name']}' event is not permitted")

            response_json = {
                "status": "success",
                "data": payload_json["data"],
                "id": event_id,
            }
        except (json.JSONDecodeError, ValueError) as exp:
            logging.error(exp)
            response_json = {
                "status": "error",
                "detail": "unexpected server error",
                "id": event_id,
            }

        self.__inbox.append(json.dumps(response_json))

    def recv(self, timeout: float | None = None, decode: bool | None = None) -> Data:
        """Receive the next mock response."""
        if not self._connected:
            raise websockets.ConnectionClosed(
                rcvd=websockets.Close(code=1008, reason="socket is already closed."),
                sent=None,
            )
        return self.__inbox.pop()

    def close(
        self, code: CloseCode | int = CloseCode.NORMAL_CLOSURE, reason: str = ""
    ) -> None:
        """Shutdown the connection."""
        self._connected = False


def mock_sync_connect(uri: str, **kwargs) -> MockWebsocket:
    """Replacement for websockets.sync.client.connect used in tests.

    Mirrors what the real connect() does: constructs the connection and then
    calls handshake() so that _connected is True before the caller sends data.
    """
    ws = MockWebsocket(uri=uri, **kwargs)
    ws.handshake(additional_headers=kwargs.get("additional_headers"))
    return ws


async def mock_mss_websocket_handler(websocket: websockets.ServerConnection):
    """A mock handler for the websockets at MSS

    Args:
        websocket: The websocket ServerConnection object
    """
    if not _WS_PATH_PATTERN.match(websocket.request.path):
        await websocket.close(code=1000, reason="Invalid Path")
        return

    headers = websocket.request.headers
    try:
        _verify_headers(headers)
    except websockets.WebSocketException as exp:
        await websocket.close(code=1008, reason=f"{exp}")
        return

    try:
        payload = await websocket.recv()
    except websockets.ConnectionClosed:
        return

    try:
        payload_json = json.loads(payload)
        if payload_json["name"] not in _MESSAGE_TYPES:
            raise ValueError(f"'{payload_json['name']}' event is not permitted")

        response_json = {"status": "success", "data": payload_json["data"]}
    except (json.JSONDecodeError, ValueError) as exp:
        logging.error(exp)
        response_json = {"status": "error", "detail": "unexpected server error"}

    await websocket.send(json.dumps(response_json))


def _verify_headers(headers):
    """Verifies that the headers are correct

    Args:
        headers: The headers to verify

    Raises:
        websockets.WebSocketException: unauthorized
    """
    bcc_nonce_ttl = 300
    try:
        name = headers["x-id"]
        nonce = headers["x-request-id"]
        timestamp = headers["x-timestamp"]
        signature = headers["x-signature"]

        message = f"{name}-{nonce}-{timestamp}"
        # verify_ws_signature(
        #         signature=signature,
        #         message=message,
        #         key_path=backend_conf.public_key_path,
        # )

        current_timestamp = datetime.now().timestamp()
        timestamp_float = float(timestamp)
        time_difference = current_timestamp - timestamp_float
        if time_difference > bcc_nonce_ttl:
            raise ValueError(
                f"nonce of timestamp {timestamp} is {time_difference}s older than {bcc_nonce_ttl} seconds"
            )
        elif time_difference < 0:
            raise ValueError(f"timestamp {timestamp} is in the future")

    except (KeyError, InvalidSignature, ValueError, AttributeError) as exp:
        logging.error(exp)
        raise websockets.WebSocketException("unauthorized")
