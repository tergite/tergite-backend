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
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import Depends, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import FileResponse
from pydantic import ValidationError

import settings
from app.services import scheduler
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

from ..libs.queues.dtos import QueueContext, get_queue_context
from ..services.scheduler.dtos import RecalibrationInfo
from ..services.scheduler.queues import QueuePool, get_queue_pool
from ..utils.api import (
    GeneralMessage,
    RequestLog,
    get_request_logs_store,
    to_http_error,
)
from ..utils.redis_store import ItemNotFoundError
from ..utils.strings import uuid_str
from .dependencies import (
    get_mss_token_claims_dep,
    get_queue_context_if_online,
    get_verified_mss_admin_user_id,
    get_verified_mss_user_id,
    lifespan,
)
from .device_routing import DeviceRoutingMiddleware
from .routers import bookings, devices, jobs, users

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


app.include_router(devices.router)
app.include_router(users.router)
app.include_router(bookings.router)
app.include_router(jobs.router)


@app.get(
    "/logfiles/{logfile_id}",
    dependencies=[Depends(get_mss_token_claims_dep(job_id_field="logfile_id"))],
)
async def download_logfile(logfile_id: UUID):
    """Downloads the job logfile

    Args:
        logfile_id: the id of the logfile usually the job id

    Raises:
        ItemNotFoundError: logfile {logfile_id} not found
    """
    file = (settings.LOG_FILE_POOL / str(logfile_id)).with_suffix(".hdf5")
    if file.exists():
        return FileResponse(file)
    raise ItemNotFoundError(f"logfile {logfile_id} not found")


@app.post("/recalibration/init", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def initialize_recalibration(
    context: QueueContext = Depends(get_queue_context_if_online),
    queue_pool: QueuePool = Depends(get_queue_pool),
    start_timestamp: Optional[datetime] = Query(None),
    interval: Optional[float] = Query(None),
) -> GeneralMessage:
    """Initializes recalibration

    Only MSS admin users can initialize recalibration

    Args:
        context: the queue context for the recalibration
        queue_pool: the collection of queues to run the recalibration on
        start_timestamp: the start time of the recalibration; default = None i.e. straight away
        interval: the recalibration interval; default = None i.e. no repeat

    Returns:
        the status of the recalibration
    """
    return scheduler.init_recalibration(
        context, queues=queue_pool, start_timestamp=start_timestamp, interval=interval
    )


@app.get("/recalibration/info", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def get_recalibration_info(
    queue_pool: QueuePool = Depends(get_queue_pool),
) -> RecalibrationInfo:
    """Gets the current information about the recalibration

    Only MSS admin users can get recalibration info

    Args:
        queue_pool: the collection of queues to run the recalibration on

    Returns:
        the information about the recalibration
    """
    return scheduler.get_recalibration_info(queues=queue_pool)


@app.post(
    "/recalibration/cancel", dependencies=[Depends(get_verified_mss_admin_user_id)]
)
async def cancel_recalibration(
    context: QueueContext = Depends(get_queue_context_if_online),
    queue_pool: QueuePool = Depends(get_queue_pool),
    ignore_errors: bool = Query(False),
) -> GeneralMessage:
    """Cancels recalibration on this device

    Only MSS admin users can initialize recalibration

    Args:
        context: the queue context for the recalibration
        queue_pool: the collection of queues to run the recalibration on
        ignore_errors: whether to ignore errors during recalibration

    Returns:
        general information on the status of the request
    """
    scheduler.stop_recalibration(
        context, queues=queue_pool, ignore_errors=ignore_errors
    )
    return {"status": "success"}


# plain def, so FastAPI runs them in a thread: telling MSS blocks, for up to minutes
@app.post("/switch/off", dependencies=[Depends(get_verified_mss_admin_user_id)])
def switch_off(
    context: QueueContext = Depends(get_queue_context_if_online),
) -> GeneralMessage:
    """Switches off this device

    Only MSS admin users can switch on and off this device

    Args:
        context: the queue context for the recalibration

    Returns:
        general information on the status of the request
    """
    scheduler.switch_off(context)
    return {"status": "success"}


@app.post("/switch/on", dependencies=[Depends(get_verified_mss_admin_user_id)])
def switch_on(
    context: QueueContext = Depends(get_queue_context),
) -> GeneralMessage:
    """Switches on this device

    Only MSS admin users can switch on and off this device

    Args:
        context: the queue context for the recalibration

    Returns:
        general information on the status of the request
    """
    scheduler.switch_on(context)
    return {"status": "success"}


@app.get("/switch/status", dependencies=[Depends(get_verified_mss_user_id)])
async def get_switch_status(
    context: QueueContext = Depends(get_queue_context),
):
    """Gets the status of this device

    Args:
        context: the queue context for the recalibration

    Returns:
        general information on the status of the request
    """
    status = "OFF" if scheduler.is_offline(context) else "ON"
    return {"status": status}
