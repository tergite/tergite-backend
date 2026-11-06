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
"""The HTTP status code the API answers each error with"""

import logging
from typing import Any, Awaitable, Callable, Dict, Type

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from pydantic import ValidationError

from utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
    InvalidBookingError,
    InvalidCircuitError,
    InvalidJobIdInUploadedFileError,
    InvalidRequestError,
    IsOfflineError,
    ItemNotFoundError,
    JobAlreadyCancelled,
    JobAlreadyCompleteError,
    MaxBookingsError,
    MssRejectedError,
    MssTimeoutError,
    NotAuthenticatedError,
    UnauthorizedError,
)

from .dtos import InvalidCircuitDetail

HTTP_STATUS_CODES: Dict[Type[Exception], int] = {
    NotAuthenticatedError: 401,
    UnauthorizedError: 403,
    ItemNotFoundError: 404,
    JobAlreadyCompleteError: 406,
    ConflictError: 409,
    MaxBookingsError: 400,
    InvalidRequestError: 400,
    BookingAlreadyCompleteError: 400,
    BookingAlreadyActiveError: 400,
    ValidationError: 422,
    InvalidBookingError: 422,
    InvalidCircuitError: 422,
    ValueError: 500,
    IndexError: 500,
    TypeError: 500,
    RuntimeError: 500,
    TimeoutError: 500,
    InvalidJobIdInUploadedFileError: 400,
    JobAlreadyCancelled: 406,
    IsOfflineError: 503,
    MssRejectedError: 502,
    MssTimeoutError: 504,
}


def add_error_handlers(app: FastAPI) -> None:
    """Answers each error in HTTP_STATUS_CODES with its status code

    Args:
        app: the FastAPI application to add the error handlers to
    """
    for error, status_code in HTTP_STATUS_CODES.items():
        app.add_exception_handler(error, _to_http_error(status_code))


def _to_http_error(
    status_code: int,
) -> Callable[[Request, Exception], Awaitable[Response]]:
    """An error handler that logs the error and answers with the status code"""

    async def handle(request: Request, exp: Exception) -> Response:
        logging.error(exp)
        detail = _get_detail(exp, status_code=status_code)
        return await http_exception_handler(request, HTTPException(status_code, detail))

    return handle


def _get_detail(exp: Exception, status_code: int) -> Any:
    """The detail of the error's response: its message, or the fields a client reads"""
    if status_code == 500:
        # a server error's own message may tell internals
        return "Unexpected server error"
    if isinstance(exp, InvalidCircuitError):
        return InvalidCircuitDetail(
            message=f"{exp}",
            circuit_index=exp.circuit_index,
            line=exp.line,
            column=exp.column,
        ).model_dump()
    return f"{exp}"
