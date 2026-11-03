# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Parameters that many tests are parametrized with"""
import importlib.util
from typing import Any, Dict, List, NotRequired, Optional, TypedDict

from pytest_lazy_fixtures import lf as lazy_fixture

from tests.utils.fixtures import load_fixture

HAS_QISKIT_DYNAMICS = importlib.util.find_spec("qiskit_dynamics") is not None
HAS_QUANTIFY = importlib.util.find_spec("quantify_scheduler") is not None

FASTAPI_CLIENTS = []
CLIENT_AND_WORKER_TUPLES = []
# those of the device on clusters alone, which recalibrates, as the simulated ones do not
QUANTIFY_CLIENT_AND_WORKER_TUPLES = []
if HAS_QUANTIFY:
    FASTAPI_CLIENTS += [lazy_fixture("quantify_rest_client")]
    QUANTIFY_CLIENT_AND_WORKER_TUPLES += [
        (
            lazy_fixture("quantify_rest_client"),
            lazy_fixture("quantify_worker"),
        ),
    ]
    CLIENT_AND_WORKER_TUPLES += QUANTIFY_CLIENT_AND_WORKER_TUPLES
if HAS_QUANTIFY:
    FASTAPI_CLIENTS += [
        lazy_fixture("qiskit_1q_rest_client"),
        lazy_fixture("qiskit_2q_rest_client"),
    ]
    CLIENT_AND_WORKER_TUPLES += [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            lazy_fixture("qiskit_1q_worker"),
        ),
        (
            lazy_fixture("qiskit_2q_rest_client"),
            lazy_fixture("qiskit_2q_worker"),
        ),
    ]

USERS: List[Dict[str, Any]] = load_fixture("users.json")
VALID_BOOKINGS: List["BasicBookingInfo"] = load_fixture("valid_bookings.json")
INVALID_BOOKINGS: List["BasicBookingInfo"] = load_fixture("invalid_bookings.json")
PAGINATION: List["PaginationInfo"] = load_fixture("pagination.json")
VALID_CREATE_BOOKINGS_PARAMS = [
    (USERS[0], booking, client)
    for booking in VALID_BOOKINGS
    for client in FASTAPI_CLIENTS
]
INVALID_CREATE_BOOKINGS_PARAMS = [
    (USERS[0], booking, client)
    for booking in INVALID_BOOKINGS
    for client in FASTAPI_CLIENTS
]

SIM_1Q_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload_simulator_1q.json")
SIM_2Q_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload_simulator_2q.json")
QUANTIFY_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload.json")
INVALID_JOBS_FOR_UPLOAD = load_fixture("invalid_jobs_to_upload.json")

# executor-specific params
QUANTIFY_UPLOAD_JOB_PARAMS = []
SIM_1Q_UPLOAD_JOB_PARAMS = []
SIM_2Q_UPLOAD_JOB_PARAMS = []
STATIC_PROPERTIES_PARAMS = []
DYNAMIC_PROPERTIES_PARAMS = []

if HAS_QUANTIFY:
    QUANTIFY_UPLOAD_JOB_PARAMS = [
        (
            lazy_fixture("quantify_rest_client"),
            lazy_fixture("quantify_worker"),
            job,
        )
        for job in QUANTIFY_JOBS_FOR_UPLOAD
    ]
    STATIC_PROPERTIES_PARAMS += [
        (lazy_fixture("quantify_rest_client"), load_fixture("static_properties.json"))
    ]
    DYNAMIC_PROPERTIES_PARAMS += [
        (lazy_fixture("quantify_rest_client"), load_fixture("dynamic_properties.json"))
    ]

if HAS_QUANTIFY:
    SIM_1Q_UPLOAD_JOB_PARAMS = [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            lazy_fixture("qiskit_1q_worker"),
            job,
        )
        for job in SIM_1Q_JOBS_FOR_UPLOAD
    ]

    SIM_2Q_UPLOAD_JOB_PARAMS = [
        (
            lazy_fixture("qiskit_2q_rest_client"),
            lazy_fixture("qiskit_2q_worker"),
            job,
        )
        for job in SIM_2Q_JOBS_FOR_UPLOAD
    ]
    STATIC_PROPERTIES_PARAMS += [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            load_fixture("static_properties.simq1.json"),
        ),
        (
            lazy_fixture("qiskit_2q_rest_client"),
            load_fixture("static_properties.simq2.json"),
        ),
    ]
    DYNAMIC_PROPERTIES_PARAMS += [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            load_fixture("dynamic_properties.simq1.json"),
        ),
        (
            lazy_fixture("qiskit_2q_rest_client"),
            load_fixture("dynamic_properties.simq2.json"),
        ),
    ]

SIMPLE_UPLOAD_JOB_PARAMS = (
    QUANTIFY_UPLOAD_JOB_PARAMS + SIM_1Q_UPLOAD_JOB_PARAMS + SIM_2Q_UPLOAD_JOB_PARAMS
)

ALL_UPLOAD_JOB_PARAMS = (
    [(*args, {"0x0": 750}, "system_test") for args in QUANTIFY_UPLOAD_JOB_PARAMS]
    + [(*args, {"0x0": 750}, "qiskit_pulse_1q") for args in SIM_1Q_UPLOAD_JOB_PARAMS]
    + [
        (*args, {"0x0": 400, "0x3": 400}, "qiskit_pulse_2q")
        for args in SIM_2Q_UPLOAD_JOB_PARAMS
    ]
    # the bell state of 00 and 11 == ~512
)

# client, worker, job, device_name
VIEW_JOBS_PARAMS = (
    [(*args, "system_test") for args in QUANTIFY_UPLOAD_JOB_PARAMS]
    + [(*args, "qiskit_pulse_1q") for args in SIM_1Q_UPLOAD_JOB_PARAMS]
    + [(*args, "qiskit_pulse_2q") for args in SIM_2Q_UPLOAD_JOB_PARAMS]
)

# client, worker, job, device_name, user
VIEW_JOB_PARAMS = [
    (client, worker, job, device_name, user)
    for (client, worker, job, device_name) in VIEW_JOBS_PARAMS
    for user in USERS[:2]
]

ALL_INVALID_UPLOAD_JOB_PARAMS = [
    (client, worker, job)
    for job in INVALID_JOBS_FOR_UPLOAD
    for client, worker in CLIENT_AND_WORKER_TUPLES
]

VIEW_MANY_PARAMS = [
    (pagination, client) for pagination in PAGINATION for client in FASTAPI_CLIENTS
]


class BasicBookingInfo(TypedDict):
    """The simplified basic booking info"""

    starts_in: float
    duration: float
    error_message: NotRequired[str]


class PaginationInfo(TypedDict):
    """The pagination info"""

    skip: int
    limit: Optional[float]
