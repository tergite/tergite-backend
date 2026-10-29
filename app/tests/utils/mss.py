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
import base64
import json
import logging
import threading
from collections.abc import Iterable
from datetime import datetime
from typing import (
    Any,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Union,
)

import pytest
import websockets
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from pytest_mock import MockerFixture
from websockets import ClientProtocol, HeadersLike
from websockets.frames import CloseCode
from websockets.http11 import USER_AGENT
from websockets.sync.client import ClientConnection
from websockets.typing import DataLike
from websockets.uri import parse_uri

from app.tests.utils.env import TEST_PUBLIC_KEY_FILE

_MESSAGE_TYPES = (
    "initialized",
    "recalibrated",
    "job_updated",
    "switched_on",
    "switched_off",
)

type Data = Union[bytes, str]

_default_protocol = ClientProtocol(uri=parse_uri("ws://localhost:8000"))


class MockMss:
    """The MSS end of the websockets the BCC opens: it records the events sent on each, and
    can refuse an event, stay silent, or drop a connection while the BCC waits, as MSS may
    """

    def __init__(self):
        self.connections: List["MockWebsocket"] = []
        self.rejected_event_names: Set[str] = set()
        self.is_silent = False
        self.should_drop_next_recv = False

    def connect(self, uri: str, **kwargs: Any) -> "MockWebsocket":
        """Replaces websockets.sync.client.connect, handshaking as it does"""
        connection = MockWebsocket(mss=self, uri=uri, **kwargs)
        connection.handshake(additional_headers=kwargs.get("additional_headers"))
        self.connections.append(connection)
        return connection

    @property
    def sent_events(self) -> List[Dict[str, Any]]:
        """the events sent to MSS on every connection, in order"""
        return [
            event for connection in self.connections for event in connection.sent_events
        ]


class MockWebsocket(ClientConnection):
    """Sync mock of websockets.sync.client.ClientConnection for testing MSS."""

    def __init__(
        self, sock=None, protocol=_default_protocol, *args, mss: MockMss, **kwargs
    ):
        # Skip super().__init__() — it starts a recv_events thread that
        # performs real socket I/O; our mock overrides all I/O methods.
        self.__mss = mss
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
            if payload_json["name"] in self.__mss.rejected_event_names:
                raise ValueError(f"'{payload_json['name']}' events are rejected")

            response_json = {
                "status": "success",
                "data": payload_json["data"],
                "id": event_id,
            }
        except (json.JSONDecodeError, ValueError) as exp:
            logging.error(exp)
            response_json = {
                "status": "error",
                "detail": f"{exp}",
                "id": event_id,
            }

        if not self.__mss.is_silent:
            self.__inbox.append(json.dumps(response_json))

    @property
    def sent_events(self) -> List[Dict[str, Any]]:
        """the events sent on this connection, in order"""
        return [json.loads(message) for message in self.__outbox]

    def recv(self, timeout: float | None = None, decode: bool | None = None) -> Data:
        """Receive the oldest mock response, or time out as a socket does without one"""
        if self.__mss.should_drop_next_recv:
            self.__mss.should_drop_next_recv = False
            self._connected = False
        if not self._connected:
            raise websockets.ConnectionClosed(
                rcvd=websockets.Close(code=1008, reason="socket is already closed."),
                sent=None,
            )
        if not self.__inbox:
            raise TimeoutError("timed out while waiting for a message")
        return self.__inbox.pop(0)

    def close(
        self, code: CloseCode | int = CloseCode.NORMAL_CLOSURE, reason: str = ""
    ) -> None:
        """Shutdown the connection."""
        self._connected = False


@pytest.fixture
def mock_mss(mocker) -> MockMss:
    """The MSS that the BCC's websocket reaches"""
    mss = MockMss()
    mocker.patch("app.services.external.mss.service.connect", side_effect=mss.connect)
    return mss


@pytest.fixture
def patched_mss_websockets(mocker, mock_mss) -> Generator[MockerFixture, Any, None]:
    """Patch the websocket used to connect to MSS"""
    yield mocker


def _verify_headers(headers):
    """Verifies that the headers are correct

    Args:
        headers: The headers to verify

    Raises:
        websockets.WebSocketException: unauthorized
    """
    bcc_nonce_ttl = 300
    try:
        timestamp = headers["x-timestamp"]
        message = f"{headers['x-id']}-{headers['x-request-id']}-{timestamp}"
        _get_bcc_public_key().verify(
            base64.b64decode(headers["x-signature"]),
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256(),
        )

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


def _get_bcc_public_key():
    with open(TEST_PUBLIC_KEY_FILE, "rb") as file:
        return serialization.load_pem_public_key(file.read())
