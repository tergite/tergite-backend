# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright Abdullah-Al Amin 2022
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Modified:
#
# - Martin Ahindura 2023

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from pydantic import ValidationError

import settings
from app.utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
    InvalidJobIdInUploadedFileError,
    InvalidRequestError,
    IsOfflineError,
    JobAlreadyCancelled,
    JobAlreadyCompleteError,
    MaxBookingsError,
    MssRejectedError,
    MssTimeoutError,
    NotAuthenticatedError,
    UnauthorizedError,
)

from ..utils.api import RequestLog, get_request_logs_store, to_http_error
from ..utils.redis_store import ItemNotFoundError
from ..utils.strings import uuid_str
from .dependencies import get_verified_mss_user_id, lifespan
from .device_routing import DeviceRoutingMiddleware
from .routers import bookings, devices, jobs, logfiles, recalibration, switch, users

# application
app = FastAPI(
    title="Backend Control Computer",
    description="Interfaces Quantum processor via REST API",
    version="v2026.09.0-rc.1",
    lifespan=lifespan,
)

# exception handlers
app.add_exception_handler(NotAuthenticatedError, to_http_error(401))
app.add_exception_handler(UnauthorizedError, to_http_error(403))
app.add_exception_handler(ItemNotFoundError, to_http_error(404))
app.add_exception_handler(JobAlreadyCompleteError, to_http_error(406))
app.add_exception_handler(ConflictError, to_http_error(409))
app.add_exception_handler(MaxBookingsError, to_http_error(400))
app.add_exception_handler(InvalidRequestError, to_http_error(400))
app.add_exception_handler(BookingAlreadyCompleteError, to_http_error(400))
app.add_exception_handler(BookingAlreadyActiveError, to_http_error(400))
app.add_exception_handler(ValidationError, to_http_error(422))
app.add_exception_handler(ValueError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(IndexError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(TypeError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(RuntimeError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(TimeoutError, to_http_error(500, "Unexpected server error"))
app.add_exception_handler(InvalidJobIdInUploadedFileError, to_http_error(400))
app.add_exception_handler(JobAlreadyCancelled, to_http_error(406))
app.add_exception_handler(IsOfflineError, to_http_error(503))
app.add_exception_handler(MssRejectedError, to_http_error(502))
app.add_exception_handler(MssTimeoutError, to_http_error(504))

# setup CORS if any device's CORS_ORIGINS are set
cors_origins = sorted(
    {
        origin
        for device in settings.get_device_names()
        for origin in settings.get_device_settings(device).CORS_ORIGINS
    }
)
if len(cors_origins) > 0:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Adds an `x-request-id` header and logs requests received

    It will get it from `x-mss-request-id` if that is present or generate a new one

    Args:
        request: the current FastAPI request object
        call_next: the callback that calls the next middleware or route handler
    """
    request_id = request.headers.get("x-mss-request-id")
    if request_id is None:
        request_id = uuid_str()

    request.state.request_id = request_id

    response = await call_next(request)

    requests_store = get_request_logs_store()
    if not requests_store.exists(request_id):
        requests_store.insert(RequestLog.from_request(request))

    response.headers["X-Request-ID"] = request_id
    return response


# routing
# added last, so outermost: the other middlewares and the routes run as the path's device
app.add_middleware(DeviceRoutingMiddleware)


@app.get("/", dependencies=[Depends(get_verified_mss_user_id)])
async def root():
    return {"message": "Welcome to BCC machine"}


app.include_router(logfiles.router)
app.include_router(devices.router)
app.include_router(users.router)
app.include_router(bookings.router)
app.include_router(jobs.router)
app.include_router(recalibration.router)
app.include_router(switch.router)
