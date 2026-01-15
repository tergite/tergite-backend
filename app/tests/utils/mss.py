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
from typing import Dict, List, Optional, Tuple, Union

import websocket
from websocket import ABNF


class MockWebsocket(websocket.WebSocket):
    """A mock of the websocket.WebSocket"""

    def __init__(self, message_types: tuple[str, ...], **kwargs):
        super().__init__(**kwargs)
        self.__message_types = message_types
        self.__url: Optional[str] = None
        self.__conn_kwargs: Dict[str, str] = {}
        self.__outbox: List[Tuple[Union[bytes, str], int]] = []
        self.__inbox: List[str] = []

    def connect(self, url: str, **kwargs):
        """Connects to the given url"""
        if self.connected:
            raise RuntimeError("Connection already established")

        self.__url = url
        self.__conn_kwargs = kwargs
        self.__outbox = []
        self.__inbox = []
        self.connected = True

    def init_connection(self, url: str, **kwargs):
        """Initializes a new connection and returns itself

        Args:
            url: The url to connect to
            **kwargs: Additional arguments to pass to the websocket constructor

        Returns:
            the WebSocket instance
        """
        self.connect(url, **kwargs)
        return self

    def send(self, payload: Union[bytes, str], opcode: int = ABNF.OPCODE_TEXT) -> int:
        """Send a payload and returns the frame length"""
        if not self.connected:
            raise websocket.WebSocketConnectionClosedException(
                "socket is already closed."
            )

        self.__outbox.append((payload, opcode))
        try:
            payload_json = json.loads(payload)
            if payload_json["name"] not in self.__message_types:
                raise ValueError(f"'{payload_json['name']}' event is not permitted")

            response_json = {"status": "success", "data": payload_json["data"]}
        except (json.JSONDecodeError, ValueError) as exp:
            logging.error(exp)
            response_json = {"status": "error", "detail": "unexpected server error"}

        self.__inbox.append(json.dumps(response_json))
        # FIXME: Just mocking frame length by getting number of items in payload
        return len(payload)

    def recv(self) -> Union[bytes, str]:
        """Receive a payload and returns the opcode"""
        if not self.connected:
            raise websocket.WebSocketConnectionClosedException(
                "socket is already closed."
            )
        return self.__inbox.pop()

    def shutdown(self):
        """Shutdown the connection"""
        self.connected = False
