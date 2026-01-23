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
from datetime import datetime

import websockets
from cryptography.exceptions import InvalidSignature

_MESSAGE_TYPES = ("initialized", "recalibrated", "job_updated", "ping")
_WS_PATH_PATTERN = re.compile(r"/devices/ws/(?P<name>[a-zA-Z0-9_-]+)")


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
