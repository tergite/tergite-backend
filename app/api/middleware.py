# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Gives each request an id"""
from typing import Awaitable, Callable

from fastapi import Request, Response

from ..utils.strings import uuid_str


async def add_request_id(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Adds an `x-request-id` header, MSS's `x-mss-request-id` if it sent one, else a new one

    Args:
        request: the current FastAPI request object
        call_next: the callback that calls the next middleware or route handler
    """
    request_id = request.headers.get("x-mss-request-id")
    if request_id is None:
        request_id = uuid_str()

    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
