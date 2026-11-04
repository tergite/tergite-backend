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
from pydantic import TypeAdapter

import settings
from services.external.mss import DeviceEvent, EventResponse
from services.scheduler import JobFile

from .dependencies import get_verified_mss_user_id, lifespan
from .device_routing import DeviceRoutingMiddleware
from .errors import add_error_handlers
from .middleware import add_request_id
from .routers import bookings, devices, jobs, logfiles, recalibration, switch, users

# where the spec's components are, for the schemas added to them
_REF = "#/components/schemas/{model}"

# application
app = FastAPI(
    title="Backend Control Computer",
    description="Interfaces Quantum processor via REST API",
    version="v2026.11.0-rc.7",
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

app.middleware("http")(add_request_id)

# routing
# added last, so outermost: the other middlewares and the routes run as the path's device
app.add_middleware(DeviceRoutingMiddleware)


@app.get("/", dependencies=[Depends(get_verified_mss_user_id)])
async def root():
    """Greets a caller that MSS has signed for, to check it can reach this device

    Returns:
        a welcome message
    """
    return {"message": "Welcome to BCC machine"}


@app.get("/health")
async def check_health() -> dict[str, str]:
    """Answers that the API runs, for bcc start and load balancers, without MSS's signature

    Returns:
        the status "ok"
    """
    return {"status": "ok"}


app.include_router(logfiles.router)
app.include_router(devices.router)
app.include_router(users.router)
app.include_router(bookings.router)
app.include_router(jobs.router)
app.include_router(recalibration.router)
app.include_router(switch.router)

_get_routes_openapi = app.openapi


def get_openapi() -> dict:
    """Gets the API's OpenAPI schema, whose components also hold what no route's parameters
    name: the job file that a job's upload carries, and the events the backend sends MSS over
    its websocket, with MSS's reply to each, from which MSS generates its models of them

    Returns:
        the schema
    """
    if app.openapi_schema is None:
        schemas = _get_routes_openapi()["components"]["schemas"]
        _add_schema(schemas, "JobFile", JobFile.model_json_schema(ref_template=_REF))
        _add_schema(
            schemas,
            "DeviceEvent",
            DeviceEvent.model_json_schema(mode="serialization", ref_template=_REF),
        )
        _add_schema(
            schemas,
            "EventResponse",
            TypeAdapter(EventResponse).json_schema(ref_template=_REF),
        )
    return app.openapi_schema


app.openapi = get_openapi


def _add_schema(schemas: dict, name: str, schema: dict) -> None:
    """Adds the schema, and the schemas it refers to that the routes' do not already define,
    to the spec's components; those they do differ only in leaving out null defaults"""
    for ref_name, ref_schema in schema.pop("$defs", {}).items():
        schemas.setdefault(ref_name, ref_schema)
    schemas[name] = schema
