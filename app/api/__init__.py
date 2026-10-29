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

import settings

from .dependencies import get_verified_mss_user_id, lifespan
from .device_routing import DeviceRoutingMiddleware
from .errors import add_error_handlers
from .middleware import log_requests
from .routers import bookings, devices, jobs, logfiles, recalibration, switch, users

# application
app = FastAPI(
    title="Backend Control Computer",
    description="Interfaces Quantum processor via REST API",
    version="v2026.09.0-rc.1",
    lifespan=lifespan,
)

add_error_handlers(app)

# setup CORS if any device's CORS_ORIGINS are set
cors_origins = sorted(
    {
        origin
        for device in settings.get_device_names()
        for origin in settings.get_device_settings(device).cors_origins
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

app.middleware("http")(log_requests)

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
