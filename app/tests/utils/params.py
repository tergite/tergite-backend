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

from app.tests.utils.fixtures import load_fixture

HAS_QISKIT_DYNAMICS = importlib.util.find_spec("qiskit_dynamics") is not None
HAS_QUANTIFY = importlib.util.find_spec("quantify_scheduler") is not None

FASTAPI_CLIENTS = []
CLIENT_AND_RQ_WORKER_TUPLES = []
if HAS_QUANTIFY:
    FASTAPI_CLIENTS += [lazy_fixture("quantify_rest_client")]
    CLIENT_AND_RQ_WORKER_TUPLES += [
        (
            lazy_fixture("quantify_rest_client"),
            lazy_fixture("redis_client"),
            lazy_fixture("rq_worker"),
        ),
    ]
if HAS_QISKIT_DYNAMICS:
    FASTAPI_CLIENTS += [
        lazy_fixture("qiskit_1q_rest_client"),
        lazy_fixture("qiskit_2q_rest_client"),
    ]
    CLIENT_AND_RQ_WORKER_TUPLES += [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            lazy_fixture("redis_client"),
            lazy_fixture("rq_worker_for_simulator_1q"),
        ),
        (
            lazy_fixture("qiskit_2q_rest_client"),
            lazy_fixture("redis_client"),
            lazy_fixture("rq_worker_for_simulator_2q"),
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


class BasicBookingInfo(TypedDict):
    """The simplified basic booking info"""

    starts_in: float
    duration: float
    error_message: NotRequired[str]


class PaginationInfo(TypedDict):
    """The pagination info"""

    skip: int
    limit: Optional[float]
