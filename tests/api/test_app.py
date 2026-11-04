# This code is part of Tergite
#
# (C) Martin Ahindura 2023, 2024
# (C) Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Tests for what every route of the API does: MSS's signed headers, request ids, errors and an offline device"""

import asyncio
import threading
import time
from uuid import uuid4

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from tests.utils.api import (
    create_invalid_mss_headers,
    create_mss_headers,
    create_mss_signature,
    get_status_codes_as_mss_admin,
    get_test_client,
)
from tests.utils.env import (
    TEST_MSS_NONCE_TTL,
)
from tests.utils.params import (
    FASTAPI_CLIENTS,
)
from utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
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
from utils.strings import validate_uuid4_str

_OFFLINE_REFUSED_ROUTES = [
    ("DELETE", "/me"),
    ("DELETE", f"/users/{uuid4()}"),
    ("POST", "/bookings"),
    ("POST", f"/bookings/{uuid4()}/cancel"),
    ("POST", "/jobs"),
    ("GET", "/jobs"),
    ("GET", f"/jobs/{uuid4()}"),
    ("POST", f"/jobs/{uuid4()}/cancel"),
    ("DELETE", f"/jobs/{uuid4()}"),
    ("POST", "/recalibration/init"),
    ("POST", "/recalibration/cancel"),
    ("POST", "/switch/off"),
]
# these read, or touch only the bookings database, so they answer an offline device
_OFFLINE_ANSWERED_ROUTES = [
    ("GET", "/"),
    ("GET", "/me"),
    ("POST", "/token"),
    ("POST", "/users"),
    ("GET", "/users"),
    ("GET", "/bookings"),
    ("GET", "/bookings/config"),
    ("GET", "/static-properties"),
    ("GET", "/dynamic-properties"),
    ("GET", f"/logfiles/{uuid4()}"),
    ("GET", "/recalibration/info"),
    ("POST", "/switch/on"),
    ("GET", "/switch/status"),
]
_ERROR_STATUS_CODES = [
    (NotAuthenticatedError(), 401),
    (UnauthorizedError(), 403),
    (ItemNotFoundError(), 404),
    (JobAlreadyCompleteError(), 406),
    (ConflictError(), 409),
    (MaxBookingsError(), 400),
    (InvalidRequestError(), 400),
    (BookingAlreadyCompleteError(), 400),
    (BookingAlreadyActiveError(), 400),
    (ValidationError.from_exception_data("Job", []), 422),
    (ValueError(), 500),
    (IndexError(), 500),
    (TypeError(), 500),
    (RuntimeError(), 500),
    (TimeoutError(), 500),
    (InvalidJobIdInUploadedFileError(), 400),
    (JobAlreadyCancelled(), 406),
    (IsOfflineError(), 503),
    (MssRejectedError(), 502),
    (MssTimeoutError(), 504),
]
_ROOT_PARAMS = [
    (client, headers)
    for client in FASTAPI_CLIENTS
    for headers in create_invalid_mss_headers()
]


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_root(client):
    """GET / returns "message": "Welcome to BCC machine"""
    with client as client:
        headers = create_mss_headers()
        response = client.get("/", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"message": "Welcome to BCC machine"}


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_health_answers_without_mss_headers(client):
    """For bcc start and load balancers, which have no MSS signature"""
    with client as client:
        response = client.get("/health")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


@pytest.mark.parametrize("client,headers", _ROOT_PARAMS)
def test_root_invalid_headers(client, headers):
    """GET / fails with 401 unauthorized when accessed with invalid MSS headers"""
    with client as client:
        response = client.get("/", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.parametrize(
    "signed_message",
    [
        "-{nonce}-{timestamp}",
        "|{nonce}|{timestamp}|True|GET|/me",
        "|{nonce}|{timestamp}|True|POST|/users",
        "|{nonce}|{timestamp}||GET|/users",
    ],
    ids=["user-nonce-timestamp", "other path", "other method", "is_admin added"],
)
@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_mss_signature_covers_is_admin_method_and_path(client, signed_message):
    """MSS headers signed for another route, or before is_admin was set, are refused, so
    that no one between MSS and the BCC can turn on admin or move them"""
    with client as client:
        nonce, timestamp = f"{uuid4()}", f"{time.time()}"
        signature = create_mss_signature(
            signed_message.format(nonce=nonce, timestamp=timestamp)
        )
        headers = {
            "x-mss-user-id": "",
            "x-mss-request-id": nonce,
            "x-mss-timestamp": timestamp,
            "x-mss-signature": signature,
            "x-mss-is-admin": "True",
        }

        response = client.get("/users", headers=headers)

        assert response.status_code == 401


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_replayed_mss_headers_of_a_failed_request(client, mocker):
    """MSS headers are refused a second time even when the first request never finished"""
    mocker.patch("services.booking.get_user_profile", side_effect=Exception("crashed"))
    with client as client:
        headers = create_mss_headers(user_id=f"{uuid4()}")
        with pytest.raises(Exception, match="crashed"):
            client.get("/me", headers=headers)

        response = client.get("/me", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_reused_mss_nonce_is_refused(client):
    with client as client:
        headers = create_mss_headers()
        client.get("/", headers=headers)

        response = client.get("/", headers=headers)

        assert response.status_code == 401


@pytest.mark.parametrize(
    "seconds_from_now",
    [-TEST_MSS_NONCE_TTL - 10, 60],
    ids=["older than MSS_NONCE_TTL", "in the future"],
)
@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_mss_timestamp_out_of_range_is_refused(client, seconds_from_now):
    timestamp = time.time() + seconds_from_now
    with client as client:
        response = client.get("/", headers=create_mss_headers(timestamp=timestamp))

        assert response.status_code == 401


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_offline_device_refuses_the_routes_that_change_or_list_its_work(client):
    """An offline device answers 503 on the routes that check it, and the rest as usual"""
    with client as client:
        client.post("/switch/off", headers=create_mss_headers(is_admin=True))

        refused_codes = get_status_codes_as_mss_admin(client, _OFFLINE_REFUSED_ROUTES)
        answered_codes = get_status_codes_as_mss_admin(client, _OFFLINE_ANSWERED_ROUTES)

        assert refused_codes == {route: 503 for route in refused_codes}
        assert [route for route, code in answered_codes.items() if code == 503] == []


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_every_response_carries_its_request_id(client):
    """X-Request-ID is MSS's x-mss-request-id, or a new one"""
    with client as client:
        headers = create_mss_headers()
        answered = client.get("/", headers=headers)
        refused = client.get("/")
        missing = client.get("/no-such-route")

        assert answered.headers["X-Request-ID"] == headers["x-mss-request-id"]
        assert validate_uuid4_str(refused.headers["X-Request-ID"])
        assert validate_uuid4_str(missing.headers["X-Request-ID"])


@pytest.mark.parametrize("error, status_code", _ERROR_STATUS_CODES)
def test_errors_map_to_their_status_codes(error, status_code):
    from api import app

    handler = app.exception_handlers[type(error)]
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    response = asyncio.run(handler(request, error))

    assert response.status_code == status_code


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_unexpected_error_answers_500(client, mocker):
    mocker.patch(
        "services.booking.get_user_profile", side_effect=Exception("unexpected")
    )
    with client:
        server_error_client = get_test_client(
            client.app, should_raise_server_exceptions=False
        )

        response = server_error_client.get("/me", headers=create_mss_headers())

        assert response.status_code == 500


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_api_workers_start_a_device_one_at_a_time(client, mocker):
    from api import app, dependencies

    starting, overlaps = [], []

    def start_device(device):
        starting.append(None)
        overlaps.append(len(starting))
        time.sleep(0.2)
        starting.pop()

    async def start_and_stop():
        async with dependencies.lifespan(app):
            pass

    mocker.patch.object(dependencies, "_start_device", side_effect=start_device)
    workers = [
        threading.Thread(target=asyncio.run, args=(start_and_stop(),)) for _ in range(3)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert overlaps == [1, 1, 1]
