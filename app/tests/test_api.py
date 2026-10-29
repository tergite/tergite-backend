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
"""Tests for the scheduler that handles queueing of user's jobs"""
import asyncio
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from unittest.mock import Mock
from uuid import uuid4

import jwt
import pytest
import time_machine
import toml
from pydantic import ValidationError
from pytest_lazy_fixtures import lf as lazy_fixture
from pytest_mock import MockerFixture
from redis import Redis
from rq.job import Job as RqJob
from rq.timeouts import JobTimeoutException
from starlette.requests import Request

import app.services.scheduler.tasks as scheduler_tasks
from app.api.worker import preload_worker_globals
from app.libs.queues.dtos import Job, JobStatus, Stage, Timestamps, get_queue_context
from app.services.booking.models import Booking
from app.services.external.mss.service import MssClient
from app.services.scheduler.utils import get_rq_job_id
from app.tests.utils.api import (
    create_invalid_mss_headers,
    create_mss_headers,
    create_mss_signature,
    decrypt_access_token,
    get_bearer_headers,
    get_status_codes_as_mss_admin,
    get_test_client,
    get_token,
)
from app.tests.utils.bookings import (
    cancel_booking,
    create_booking,
    post_booking,
    to_booking_payload,
    view_booking_list,
)
from app.tests.utils.datetime import get_timestamp_str
from app.tests.utils.env import (
    TEST_DEFAULT_RECALIBRATION_INTERVAL,
    TEST_JWT_SECRET,
    TEST_JWT_TTL,
    TEST_MAX_IDLE_TIME,
    TEST_MAX_RECALIBRATION_QUEUE_TIME,
    TEST_MAX_SLOTS_PER_DAY,
    TEST_MAX_TIME_SLOT_LENGTH,
    TEST_MIN_TIME_SLOT_LENGTH,
    TEST_MSS_NONCE_TTL,
)
from app.tests.utils.fixtures import load_fixture
from app.tests.utils.jobs import (
    cancel_job_via_mss,
    get_job_submission_metadata,
    get_jobs_in_redis,
    get_jobs_sent_to_mss,
    get_raw_jobs,
    is_job_result_matching,
    save_job_file,
    submit_jobs,
    view_job,
)
from app.tests.utils.params import (
    CLIENT_AND_RQ_WORKER_TUPLES,
    FASTAPI_CLIENTS,
    HAS_QISKIT_DYNAMICS,
    HAS_QUANTIFY,
    INVALID_CREATE_BOOKINGS_PARAMS,
    PAGINATION,
    USERS,
    VALID_BOOKINGS,
    VALID_CREATE_BOOKINGS_PARAMS,
    BasicBookingInfo,
    PaginationInfo,
)
from app.tests.utils.records import drop_microseconds, order_by, paginate, remove_dates
from app.tests.utils.rq import get_queue_by_name, wait_on_rq_worker
from app.tests.utils.users import (
    create_many_users,
    create_user,
    delete_own_profile,
    remove_user,
    view_own_profile,
    view_user_list,
)
from app.utils.api import get_request_logs_store
from app.utils.exc import (
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
    NotAllowedError,
    NotAuthenticatedError,
    UnauthorizedError,
)
from app.utils.strings import validate_uuid4_str

_SIM_1Q_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload_simulator_1q.json")
_SIM_2Q_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload_simulator_2q.json")
_QUANTIFY_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload.json")
_INVALID_JOBS_FOR_UPLOAD = load_fixture("invalid_jobs_to_upload.json")

# executor-specific params
_QUANTIFY_UPLOAD_JOB_PARAMS = []
_SIM_1Q_UPLOAD_JOB_PARAMS = []
_SIM_2Q_UPLOAD_JOB_PARAMS = []
_STATIC_PROPERTIES_PARAMS = []
_DYNAMIC_PROPERTIES_PARAMS = []

if HAS_QUANTIFY:
    _QUANTIFY_UPLOAD_JOB_PARAMS = [
        (
            lazy_fixture("quantify_rest_client"),
            lazy_fixture("redis_client"),
            lazy_fixture("rq_worker"),
            job,
        )
        for job in _QUANTIFY_JOBS_FOR_UPLOAD
    ]
    _STATIC_PROPERTIES_PARAMS += [
        (lazy_fixture("quantify_rest_client"), load_fixture("static_properties.json"))
    ]
    _DYNAMIC_PROPERTIES_PARAMS += [
        (lazy_fixture("quantify_rest_client"), load_fixture("dynamic_properties.json"))
    ]

if HAS_QISKIT_DYNAMICS:
    _SIM_1Q_UPLOAD_JOB_PARAMS = [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            lazy_fixture("redis_client"),
            lazy_fixture("rq_worker_for_simulator_1q"),
            job,
        )
        for job in _SIM_1Q_JOBS_FOR_UPLOAD
    ]

    _SIM_2Q_UPLOAD_JOB_PARAMS = [
        (
            lazy_fixture("qiskit_2q_rest_client"),
            lazy_fixture("redis_client"),
            lazy_fixture("rq_worker_for_simulator_2q"),
            job,
        )
        for job in _SIM_2Q_JOBS_FOR_UPLOAD
    ]
    _STATIC_PROPERTIES_PARAMS += [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            load_fixture("static_properties.simq1.json"),
        ),
        (
            lazy_fixture("qiskit_2q_rest_client"),
            load_fixture("static_properties.simq2.json"),
        ),
    ]
    _DYNAMIC_PROPERTIES_PARAMS += [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            load_fixture("dynamic_properties.simq1.json"),
        ),
        (
            lazy_fixture("qiskit_2q_rest_client"),
            load_fixture("dynamic_properties.simq2.json"),
        ),
    ]

_SIMPLE_UPLOAD_JOB_PARAMS = (
    _QUANTIFY_UPLOAD_JOB_PARAMS + _SIM_1Q_UPLOAD_JOB_PARAMS + _SIM_2Q_UPLOAD_JOB_PARAMS
)

_ALL_UPLOAD_JOB_PARAMS = (
    [(*args, {"0x0": 750}, "system_test") for args in _QUANTIFY_UPLOAD_JOB_PARAMS]
    + [(*args, {"0x0": 750}, "qiskit_pulse_1q") for args in _SIM_1Q_UPLOAD_JOB_PARAMS]
    + [
        (*args, {"0x0": 400, "0x3": 400}, "qiskit_pulse_2q")
        for args in _SIM_2Q_UPLOAD_JOB_PARAMS
    ]
    # the bell state of 00 and 11 == ~512
)

# client, worker, job, device_name
_VIEW_JOBS_PARAMS = (
    [(*args, "system_test") for args in _QUANTIFY_UPLOAD_JOB_PARAMS]
    + [(*args, "qiskit_pulse_1q") for args in _SIM_1Q_UPLOAD_JOB_PARAMS]
    + [(*args, "qiskit_pulse_2q") for args in _SIM_2Q_UPLOAD_JOB_PARAMS]
)

# client, worker, job, device_name, user
_VIEW_JOB_PARAMS = [
    (client, worker, job, device_name, user)
    for (client, _, worker, job, device_name) in _VIEW_JOBS_PARAMS
    for user in USERS[:2]
]

_ALL_INVALID_UPLOAD_JOB_PARAMS = [
    (client, redis, rq_worker, job)
    for job in _INVALID_JOBS_FOR_UPLOAD
    for client, redis, rq_worker in CLIENT_AND_RQ_WORKER_TUPLES
]

# (patch target, patch kwargs) making one stage of a job fail
_FAILING_STAGE_PATCHES = [
    (
        "app.services.scheduler.tasks.decompress_qobj",
        {"side_effect": ValueError("unreadable qobj")},
    ),
    (
        "app.services.scheduler.tasks.get_quantum_executor",
        {
            "return_value": Mock(
                preprocess=Mock(return_value=(0.2, None)),
                run=Mock(side_effect=RuntimeError("device lost")),
            )
        },
    ),
    (
        "app.services.scheduler.tasks.read_job_from_hdf5",
        {"side_effect": OSError("unreadable results")},
    ),
    # RQ kills a job past its queue's timeout by raising this inside it
    (
        "app.services.scheduler.tasks.decompress_qobj",
        {"side_effect": JobTimeoutException("preprocessing timed out")},
    ),
    (
        "app.services.scheduler.tasks.get_quantum_executor",
        {
            "return_value": Mock(
                preprocess=Mock(return_value=(0.2, None)),
                run=Mock(side_effect=JobTimeoutException("execution timed out")),
            )
        },
    ),
    (
        "app.services.scheduler.tasks.read_job_from_hdf5",
        {"side_effect": JobTimeoutException("postprocessing timed out")},
    ),
]

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
    (NotAllowedError(), 405),
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

_VIEW_MANY_PARAMS = [
    (pagination, client) for pagination in PAGINATION for client in FASTAPI_CLIENTS
]

_EXPECTED_RECALIBRATION_SEED_CONTENT = load_fixture(
    "expected_recalibration.seed.toml", fmt="toml"
)


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_root(client):
    """GET / returns "message": "Welcome to BCC machine"""
    with client as client:
        headers = create_mss_headers()
        response = client.get("/", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"message": "Welcome to BCC machine"}


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
    mocker.patch(
        "app.services.booking.get_user_profile", side_effect=Exception("crashed")
    )
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
    "timestamp",
    [
        time.time() - TEST_MSS_NONCE_TTL - 10,
        time.time() + 60,
    ],
    ids=["older than MSS_NONCE_TTL", "in the future"],
)
@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_mss_timestamp_out_of_range_is_refused(client, timestamp):
    with client as client:
        response = client.get("/", headers=create_mss_headers(timestamp=timestamp))

        assert response.status_code == 401


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_token_for_another_user_is_refused(client):
    """POST '/token' with a body user_id other than MSS's x-mss-user-id answers 403"""
    with client as client:
        user_id, _ = create_user(client, user=USERS[0])
        response = client.post(
            "/token",
            json={"user_id": f"{uuid4()}", "job_id": f"{uuid4()}"},
            headers=create_mss_headers(user_id),
        )

        assert response.status_code == 403


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_token_creates_an_unknown_user(client):
    with client as client:
        user_id = f"{uuid4()}"
        get_token(client, data={"user_id": user_id, "job_id": f"{uuid4()}"})

        response = view_own_profile(client, user_id=user_id)

        assert response.status_code == 200


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_token_is_the_job_jwt_encrypted_for_mss(client):
    with client as client:
        user_id, job_id = f"{uuid4()}", f"{uuid4()}"
        response = client.post(
            "/token",
            json={"user_id": user_id, "job_id": job_id},
            headers=create_mss_headers(user_id),
        )

        token = decrypt_access_token(response.json()["access_token"])
        claims = jwt.decode(token, TEST_JWT_SECRET, algorithms=["HS256"])
        assert response.json()["token_type"] == "bearer"
        assert (claims["sub"], claims["job"]) == (user_id, job_id)


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_job_token_expires_after_jwt_ttl(client):
    with client as client:
        job_id = f"{uuid4()}"
        token, _ = get_token(client, data={"user_id": f"{uuid4()}", "job_id": job_id})

        with time_machine.travel(timedelta(seconds=TEST_JWT_TTL + 1)):
            response = client.post(
                f"/jobs/{job_id}/cancel", json={}, headers=get_bearer_headers(token)
            )

        assert response.status_code == 401


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_job_token_for_another_job_is_refused(client):
    with client as client:
        token, _ = get_token(
            client, data={"user_id": f"{uuid4()}", "job_id": f"{uuid4()}"}
        )

        response = client.post(
            f"/jobs/{uuid4()}/cancel", json={}, headers=get_bearer_headers(token)
        )

        assert response.status_code == 403


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
    """X-Request-ID is MSS's x-mss-request-id, or a new one, and the request is logged once"""
    with client as client:
        headers = create_mss_headers()
        answered = client.get("/", headers=headers)
        refused = client.get("/")
        missing = client.get("/no-such-route")

        nonce = headers["x-mss-request-id"]
        logged_ids = [log.request_id for log in get_request_logs_store().get_all()]
        assert answered.headers["X-Request-ID"] == nonce
        assert validate_uuid4_str(refused.headers["X-Request-ID"])
        assert validate_uuid4_str(missing.headers["X-Request-ID"])
        assert logged_ids.count(nonce) == 1


@pytest.mark.parametrize("error, status_code", _ERROR_STATUS_CODES)
def test_errors_map_to_their_status_codes(error, status_code):
    from app.api import app

    handler = app.exception_handlers[type(error)]
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    response = asyncio.run(handler(request, error))

    assert response.status_code == status_code


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_unexpected_error_answers_500(client, mocker):
    mocker.patch(
        "app.services.booking.get_user_profile", side_effect=Exception("unexpected")
    )
    with client:
        server_error_client = get_test_client(
            client.app, should_raise_server_exceptions=False
        )

        response = server_error_client.get("/me", headers=create_mss_headers())

        assert response.status_code == 500


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_start_up_sends_the_device_and_its_calibration_to_mss(client, mock_mss):
    with client as client:
        headers = create_mss_headers()
        static_properties = client.get("/static-properties", headers=headers).json()
        dynamic_properties = client.get(
            "/dynamic-properties", headers=create_mss_headers()
        ).json()

        [initialized, recalibrated] = mock_mss.sent_events[:2]
        assert initialized["name"] == "initialized"
        assert initialized["data"] == static_properties
        assert recalibrated["name"] == "recalibrated"
        assert recalibrated["data"] == dynamic_properties


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_switch_tells_mss_when(client, mock_mss):
    with client as client:
        client.post("/switch/off", headers=create_mss_headers(is_admin=True))
        client.post("/switch/on", headers=create_mss_headers(is_admin=True))

        [switched_off, switched_on] = mock_mss.sent_events[-2:]
        assert switched_off["name"] == "switched_off"
        assert switched_on["name"] == "switched_on"
        assert datetime.fromisoformat(switched_off["data"]["date"])
        assert datetime.fromisoformat(switched_on["data"]["date"])


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_create_user(client):
    """POST to '/users' should create the user via MSS"""
    with client as client:
        user = USERS[1]
        user_id, response = create_user(client, user=user)
        expected = {
            "id": user_id,
            "name": user["name"],
            "email": user["email"],
            "is_admin": False,
        }

        assert response.status_code == 200
        assert response.json() == expected


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_create_existing_user(client):
    """POST to '/users' with the id of a user that exists answers 409"""
    with client as client:
        user_id, _ = create_user(client, user=USERS[1])
        response = client.post(
            "/users",
            json={**USERS[1], "id": user_id},
            headers=create_mss_headers(is_admin=True),
        )

        assert response.status_code == 409


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_view_profile(client):
    """GET '/me' should show the user profile in the REST API"""
    with client as client:
        user = USERS[0]
        user_id, _ = create_user(client, user=user)

        response = view_own_profile(client, user_id=user_id)
        profile = response.json()
        expected = {
            "id": user_id,
            "name": user["name"],
            "email": user["email"],
            "is_admin": False,
        }

        assert response.status_code == 200
        assert profile == expected


@pytest.mark.parametrize("pagination, client", _VIEW_MANY_PARAMS)
def test_admin_view_users(client, pagination: "PaginationInfo"):
    """GET '/users' should show to an admin the paginated list of user profiles via MSS"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]

        skip = pagination["skip"]
        limit = pagination["limit"]

        # non admins are not allowed
        response = view_user_list(
            client, user_id=user_id, skip=skip, limit=limit, is_admin=False
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}

        # admins are allowed
        response = view_user_list(
            client, user_id=user_id, skip=skip, limit=limit, is_admin=True
        )
        actual_output = response.json()

        expected = paginate(users, skip=skip, limit=limit)

        assert response.status_code == 200
        assert actual_output == expected


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_delete_profile(
    client, worker, redis_conn, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    """DELETE '/me' should delete their own user profile"""
    with client as client:
        users = create_many_users(client, USERS[:2])
        curr_user = users[0]
        other_user = users[1]

        curr_user_id = curr_user["id"]
        other_user_id = other_user["id"]

        # create many bookings for this user
        for booking in VALID_BOOKINGS[:TEST_MAX_SLOTS_PER_DAY]:
            create_booking(client, user_id=curr_user_id, booking=booking)

        # create a booking for other user
        create_booking(
            client, user_id=other_user_id, booking={"starts_in": 8, "duration": 2}
        )

        # wait for the first booking to start
        raw_jobs = get_raw_jobs(job, durations=[0.23, 0.1, 0.23])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        submit_jobs(client, data=job_metadata_list)

        # This should also cancel any bookings and any jobs belonging to user
        response = delete_own_profile(client, user_id=curr_user_id)
        assert response.status_code == 200
        assert response.json() == {"status": "success", "detail": "Profile deleted"}

        response = view_own_profile(client, user_id=curr_user_id)
        assert response.status_code == 404
        assert response.json() == {"detail": "user not found"}

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        wait_on_rq_worker(worker, with_scheduler=True, clock=fake_clock)

        jobs_in_redis = get_jobs_in_redis(redis_conn)

        deleted_user_jobs = [
            job for job in jobs_in_redis if job.user_id == curr_user_id
        ]
        other_user_jobs = [job for job in jobs_in_redis if job.user_id != curr_user_id]
        cancellation_reason = "Cancelled on user deletion"

        assert all([v.status == JobStatus.CANCELLED for v in deleted_user_jobs])
        assert all(
            [v.cancellation_reason == cancellation_reason for v in deleted_user_jobs]
        )
        assert all([v.status == JobStatus.SUCCESSFUL for v in other_user_jobs])


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_delete_profile_cancels_executing_jobs(
    client, worker, redis_conn, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    with client as client:
        users = create_many_users(client, USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])
        # preprocessed and executed, waiting for postprocessing
        worker.work(burst=True, max_jobs=2)
        assert get_jobs_in_redis(redis_conn)[0].status == JobStatus.EXECUTING

        delete_own_profile(client, user_id=job_info["user_id"])

        assert get_jobs_in_redis(redis_conn)[0].status == JobStatus.CANCELLED


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_delete_profile_during_booking_releases_the_waitlist(
    client, worker, redis_conn, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    """Other users' jobs waiting on a booking run once its owner is deleted"""
    with client as client:
        [booker, other_user] = create_many_users(client, USERS[:2])
        create_booking(
            client, user_id=booker["id"], booking={"duration": 60, "starts_in": 0}
        )
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client,
            jobs=raw_jobs,
            users=[other_user],
            mocker=mocker,
            jobs_folder=jobs_folder,
        )[0]
        submit_jobs(client, data=[job_info])
        worker.work(burst=True)

        delete_own_profile(client, user_id=booker["id"])
        worker.work(burst=True)

        assert get_jobs_in_redis(redis_conn)[0].status == JobStatus.SUCCESSFUL


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_admin_remove_user(
    client, worker, redis_conn, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    """DELETE '/users/{user_id}' by admin removes the user, and their bookings and jobs"""
    with client as client:
        users = [curr_user, other_user] = create_many_users(client, USERS[:2])
        curr_user_id = curr_user["id"]
        other_user_id = other_user["id"]

        # create many bookings for this user
        for booking in VALID_BOOKINGS[:TEST_MAX_SLOTS_PER_DAY]:
            _, result = create_booking(client, user_id=curr_user_id, booking=booking)

        # create a booking for other user
        create_booking(
            client, user_id=other_user_id, booking={"starts_in": 8, "duration": 2}
        )

        # wait for the first booking to start
        raw_jobs = get_raw_jobs(job, durations=[0.23, 0.1, 0.23])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        submit_jobs(
            client,
            data=job_metadata_list,
        )

        # This also removes the user's pending bookings,
        # and cancels their pending jobs
        response = remove_user(
            client, current_user_id=other_user_id, user_id=curr_user_id, is_admin=True
        )
        assert response.status_code == 200
        assert response.json() == {"status": "success", "detail": "User deleted"}

        response = view_own_profile(client, user_id=curr_user_id)
        assert response.status_code == 404
        assert response.json() == {"detail": "user not found"}

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        wait_on_rq_worker(worker, with_scheduler=True, clock=fake_clock)

        jobs_in_redis = get_jobs_in_redis(redis_conn)

        deleted_user_jobs = [
            job for job in jobs_in_redis if job.user_id == curr_user_id
        ]
        other_user_jobs = [job for job in jobs_in_redis if job.user_id != curr_user_id]
        cancellation_reason = "Cancelled on user deletion"

        assert all([v.status == JobStatus.CANCELLED for v in deleted_user_jobs])
        assert all(
            [v.cancellation_reason == cancellation_reason for v in deleted_user_jobs]
        )
        assert all([v.status == JobStatus.SUCCESSFUL for v in other_user_jobs])


@pytest.mark.parametrize(
    "client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS[:-1]
)
def test_non_admin_remove_user(
    client, worker, redis_conn, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    """Non admin cannot remove the profile of any user"""
    with client as client:
        users = [curr_user, other_user] = create_many_users(client, USERS[:2])
        curr_user_id = curr_user["id"]
        other_user_id = other_user["id"]

        # create many bookings for this user
        for booking in VALID_BOOKINGS[:TEST_MAX_SLOTS_PER_DAY]:
            _, result = create_booking(client, user_id=curr_user_id, booking=booking)

        raw_jobs = get_raw_jobs(job, durations=[0.23, 0.4, 0.3])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        submit_jobs(client, data=job_metadata_list)

        # Non-admins fail
        for user_id in (curr_user_id, other_user_id):
            response = remove_user(
                client, current_user_id=user_id, user_id=curr_user_id
            )
            assert response.status_code == 403
            assert response.json() == {"detail": "Forbidden"}

        response = view_own_profile(client, user_id=curr_user_id)
        assert response.status_code == 200

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        wait_on_rq_worker(worker, with_scheduler=True, clock=fake_clock)

        jobs_in_redis = get_jobs_in_redis(redis_conn)
        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])


@pytest.mark.parametrize("user,booking,client", VALID_CREATE_BOOKINGS_PARAMS)
def test_create_booking(client, user, booking: "BasicBookingInfo"):
    """POST '/bookings' should return the new booking created by the user"""
    with client as client:
        user_id, _ = create_user(client, user=user)

        # create booking
        actual_booking_info, response = create_booking(
            client, user_id=user_id, booking=booking
        )
        actual_booking = response.json()

        assert response.status_code == 200

        assert actual_booking["id"] != ""
        assert actual_booking_info["duration"] == booking["duration"]
        assert actual_booking_info["starts_in"] == booking["starts_in"]
        assert actual_booking["user_id"] == user_id


@pytest.mark.parametrize("user,booking,client", INVALID_CREATE_BOOKINGS_PARAMS)
def test_create_invalid_booking(client, user, booking: "BasicBookingInfo"):
    """Should return an error message if an attempt to create an invalid booking is made

    e.g. start_utc in the past
    """
    with client as client:
        user_id, _ = create_user(client, user=user)
        data = to_booking_payload(booking)

        # create booking
        headers = create_mss_headers(user_id)
        response = client.post("/bookings", headers=headers, json=data)
        detail = f"{response.json()["detail"]}"

        assert response.status_code == 422
        assert booking["error_message"] in detail


@pytest.mark.parametrize("user,booking,client", VALID_CREATE_BOOKINGS_PARAMS)
def test_create_conflicting_booking(
    client, user, booking: "BasicBookingInfo", mocker: MockerFixture
):
    """Should return an error message when creating a booking overlapping with another"""
    with client as client:
        user_id, _ = create_user(client, user=user)

        # create booking
        _, response = create_booking(client, user_id=user_id, booking=booking)
        initial_slot = response.json()

        # create another
        overlapping_slot = {**booking, "duration": booking["duration"] + 10}
        headers = create_mss_headers(user_id)
        data = to_booking_payload(overlapping_slot)
        response = client.post("/bookings", headers=headers, json=data)

        start_utc = datetime.fromisoformat(initial_slot["start_utc"]).replace(
            tzinfo=None
        )
        end_utc = datetime.fromisoformat(initial_slot["end_utc"]).replace(tzinfo=None)
        expected_err_msg = (
            f"booking conflicts with another booking at {start_utc} to {end_utc}"
        )
        assert response.status_code == 409
        assert response.json() == {"detail": expected_err_msg}


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_create_too_many_booking(client):
    """Returns error message when creating a booking when number of bookings in a period for user are maxed out."""
    with client as client:
        user = USERS[1]
        user_id, _ = create_user(client, user=user)

        max_safe_idx = TEST_MAX_SLOTS_PER_DAY - 1

        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        expected_err = f"you have exceeded the maximum {TEST_MAX_SLOTS_PER_DAY} bookings per day for {day_start}-{day_end}"

        # create many bookings
        for idx, raw_item in enumerate(VALID_BOOKINGS):
            headers = create_mss_headers(user_id)
            data = to_booking_payload(raw_item)
            response = client.post("/bookings", headers=headers, json=data)
            json_response = response.json()

            if idx > max_safe_idx:
                assert expected_err in json_response["detail"]
            else:
                assert response.status_code == 200


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_unauthenticated_create_booking(client, worker, redis_conn):
    """Creating booking outside MSS errors out"""
    with client as client:
        headers = get_bearer_headers("token")
        data = to_booking_payload(VALID_BOOKINGS[0])

        response = client.post("/bookings", headers=headers, json=data)

        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        response = client.post("/bookings", json=data)

        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.parametrize("pagination,client", _VIEW_MANY_PARAMS)
def test_view_bookings(client, pagination: "PaginationInfo"):
    """GET "/bookings" shows paginated list of all available bookings, filtered accordingly"""
    with client as client:
        users = create_many_users(client)
        usr1 = users[0]
        usr2 = users[1]
        usr_id1 = usr1["id"]
        usr_id2 = usr2["id"]

        # create bookings, for each user only upto TEST_MAX_SLOTS_PER_DAY
        user_ids = [usr_id1, usr_id2]
        all_items = []
        booking_data_list = VALID_BOOKINGS[: 2 * TEST_MAX_SLOTS_PER_DAY]
        for idx, booking_info in enumerate(booking_data_list):
            ids_idx = idx % 2
            user_id = user_ids[ids_idx]
            _, response = create_booking(client, user_id=user_id, booking=booking_info)
            all_items.append(response.json())

        limit = pagination["limit"]
        skip = pagination["skip"]

        sort_by_start = lambda data, desc: order_by(data, "start_utc", desc)
        sort_by_usr = lambda data: order_by(data, "user_id")
        get_usr1_items = lambda data: [v for v in data if v["user_id"] == usr_id1]
        usr2_items = [v for v in all_items if v["user_id"] == usr_id2]
        rec2_start = all_items[1]["start_utc"]
        rec3_start = all_items[2]["start_utc"]

        min_start = "min_start_utc"
        max_start = "max_start_utc"

        # fmt: off
        filters_sort_and_results = [
            ({}, (), all_items),
            ({}, ("-start_utc",), sort_by_start(all_items, desc=True)),
            ({min_start: rec2_start}, (), all_items[1:]),
            ({min_start: rec3_start}, (), all_items[2:]),
            ({min_start: rec3_start, "user_id": usr_id1}, (), get_usr1_items(all_items[2:])),
            ({max_start: rec2_start}, (), all_items[:2]),
            ({max_start: rec2_start}, ("user_id",), sort_by_usr(all_items[:2])),
            ({max_start: rec2_start, "user_id": usr_id2}, (), usr2_items[:2]),
            ({"user_id": usr_id2}, (), usr2_items),
            ({min_start: rec2_start, max_start: rec2_start}, (), all_items[1:2]),
        ]
        # fmt: on

        # view bookings
        for user_id in user_ids:
            for filters, sort, expected_all_records in filters_sort_and_results:
                response = view_booking_list(
                    client,
                    requester_id=user_id,
                    skip=skip,
                    limit=limit,
                    sort=sort,
                    **filters,
                )
                actual_output = response.json()
                expected = paginate(expected_all_records, skip=skip, limit=limit)

                assert response.status_code == 200
                assert actual_output == expected


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_unauthenticated_view_bookings(client, worker, redis_conn):
    """Viewing bookings with non-existing user or outside MSS errors out"""
    with client as client:
        response = client.get("/bookings", headers=get_bearer_headers("token"))

        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        response = client.get("/bookings")

        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_view_bookings_config(client):
    """GET "/bookings/config" shows the configuration of the bookings service"""
    with client as client:
        user_id, _ = create_user(client, user=USERS[0])
        headers = create_mss_headers(user_id)

        got = client.get("/bookings/config", headers=headers).json()
        assert got == dict(
            max_time_slot_length=TEST_MAX_TIME_SLOT_LENGTH,
            min_time_slot_length=TEST_MIN_TIME_SLOT_LENGTH,
            max_slots_per_day=TEST_MAX_SLOTS_PER_DAY,
            max_idle_time=TEST_MAX_IDLE_TIME,
        )


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_unauthenticated_view_bookings_configs(client):
    """Viewing bookings configs with non-existing user or outside MSS errors out"""
    with client as client:
        response = client.get("/bookings/config", headers=get_bearer_headers("token"))

        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        response = client.get("/bookings/config")

        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.skipif(not HAS_QUANTIFY, reason="real simulator takes too long")
@pytest.mark.simulated
@pytest.mark.parametrize(
    "client, redis_conn, worker, job, expected_counts, device",
    _ALL_UPLOAD_JOB_PARAMS,
)
def test_submit_jobs_no_booking(
    client,
    worker,
    redis_conn,
    job,
    expected_counts,
    device,
    jobs_folder,
    mocker: MockerFixture,
    redis_conn_spy,
    mss_conn_spy,
    sql_engine_spy,
    fake_clock,
):
    """POST '/jobs' when there is no booking should run the jobs in FIFO (first in, first out)"""
    with client as client:
        # create many users and their tokens
        users = create_many_users(client)

        durations = [0.4, 3, 4, 1.1, 2.9, 0.3, 0.1]
        raw_jobs = get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        job_ids_in_fifo = submit_jobs(client, data=job_metadata_list)

        # Run the queue
        worker.work(burst=True)

        jobs_in_redis = get_jobs_in_redis(redis_conn)
        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)

        expected_jobs_in_redis = [
            Job(
                job_id=job_id,
                user_id=job_in_db.user_id,
                device=device,
                download_url=f"http://localhost:8000/logfiles/{job_id}",
                status=JobStatus.SUCCESSFUL,
                stage=Stage.FINAL_W,
                timestamps=job_in_db.timestamps,
                result=job_in_db.result,
                created_at=job_in_db.created_at,
                updated_at=job_in_db.updated_at,
                calibration_date=job_in_db.calibration_date,
                estimated_duration=job_in_db.estimated_duration,
                actual_duration=job_in_db.actual_duration,
                storage_id=f"{job_id}:::{job_in_db.estimated_duration}",
            )
            for job_id, job_in_db in zip_longest(
                job_ids_in_fifo, jobs_in_redis, fillvalue=None
            )
        ]

        # Assert that they are all complete and their timestamp of completion is in FIFO order
        assert jobs_in_redis == expected_jobs_in_redis

        user_count = len(users)
        for idx, job_in_db in enumerate(jobs_in_redis):
            user = users[idx % user_count]

            assert job_in_db.user_id == user["id"]
            assert job_in_db.estimated_duration == durations[idx]

            # Check actual duration
            assert job_in_db.actual_duration > 0

            # Check that the results are appropriate
            # This can be seen as testing gate fidelity ~70%
            assert is_job_result_matching(
                results=job_in_db.result.memory[0], expected_min_counts=expected_counts
            )

            # Check that the timestamps are appropriately filled
            timestamps = job_in_db.timestamps
            pre_processing = timestamps.pre_processing
            execution = timestamps.execution
            post_processing = timestamps.post_processing
            final = timestamps.final

            assert pre_processing.start_timestamp < pre_processing.finish_timestamp
            assert pre_processing.finish_timestamp <= execution.start_timestamp
            assert execution.start_timestamp < execution.finish_timestamp
            assert execution.finish_timestamp <= post_processing.start_timestamp
            assert post_processing.start_timestamp < post_processing.finish_timestamp
            assert post_processing.finish_timestamp <= final.start_timestamp
            assert final.start_timestamp < final.finish_timestamp

        # Connection-caching assertions: connections may be established during
        # fixture setup (0 calls) or once fresh after the pre-test cache clear
        # (1 call). Either way caching works — no per-job connection creation.
        # MSS connects lazily on the first send.
        assert redis_conn_spy.call_count <= 1
        assert mss_conn_spy.call_count <= 1
        # SQL engine: created at most once per test (initial setup); never per-job.
        assert sql_engine_spy.call_count <= 1


@pytest.mark.skipif(not HAS_QUANTIFY, reason="real simulator takes too long")
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_in_active_booking(
    client,
    worker,
    redis_conn,
    job,
    jobs_folder,
    mocker: MockerFixture,
    fake_clock,
):
    """POST '/jobs' when there is an active booking runs the booker jobs first then runs the other jobs after booking."""

    with client as client:
        # create many users and their tokens
        users = create_many_users(client)

        durations = [0.23, 2.0, 2.1, 10]
        raw_jobs = get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # create booking
        # third user; thus third job (duration: 2.1) belongs to them
        booker = users[2]
        booker_id = booker["id"]
        # duration: 3; max idle time = 1
        booking_info = {"duration": 3, "starts_in": 0}
        _, response = create_booking(client, user_id=booker_id, booking=booking_info)
        booking = Booking.model_validate(response.json())

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        wait_on_rq_worker(worker, with_scheduler=True, clock=fake_clock)

        jobs_in_redis = get_jobs_in_redis(redis_conn)
        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)

        booker_jobs = [job for job in jobs_in_redis if job.user_id == booker_id]
        non_booker_jobs = [job for job in jobs_in_redis if job.user_id != booker_id]
        last_booker_job = booker_jobs[-1]
        first_non_booker_job = non_booker_jobs[0]

        # Assert that they are all complete and their booker jobs started before booking end_utc
        # while non booker jobs started after end_utc
        # Note: Enqueue_at ignores microseconds
        booking_end_timestamp = booking.end_utc
        last_booker_job_start = last_booker_job.timestamps.execution.start_timestamp
        first_non_booker_job_start = (
            first_non_booker_job.timestamps.execution.start_timestamp
        )

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert last_booker_job_start < booking.end_utc
        assert drop_microseconds(first_non_booker_job_start) >= drop_microseconds(
            booking_end_timestamp
        )


@pytest.mark.skipif(not HAS_QUANTIFY, reason="real simulator takes too long")
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_in_idle_booking(
    client,
    worker,
    redis_conn,
    job,
    jobs_folder,
    mocker,
    fake_clock,
):
    """POST '/jobs' when there is an idle booking runs the non booker jobs even during the booking."""
    with client as client:
        # create many users and their tokens
        users = create_many_users(client)

        durations = [0.23, 2.0, 2.1, 10]
        raw_jobs = get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker = users[0]
        booker_id = booker["id"]
        booking_info = {"duration": 5, "starts_in": 0}
        _, response = create_booking(client, user_id=booker_id, booking=booking_info)
        booking = Booking.model_validate(response.json())

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        wait_on_rq_worker(worker, with_scheduler=True, clock=fake_clock)

        jobs_in_redis = get_jobs_in_redis(redis_conn)

        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        booker_jobs = [job for job in jobs_in_redis if job.user_id == booker_id]
        non_booker_jobs = [job for job in jobs_in_redis if job.user_id != booker_id]
        last_booker_job = booker_jobs[-1]
        first_non_booker_job = non_booker_jobs[0]

        # Assert that they are all complete and their booker jobs started before booking end_utc
        # and also non booker jobs started before end_utc
        # Note: Enqueue_at ignores microseconds
        booking_end_timestamp = booking.end_utc
        last_booker_job_start = last_booker_job.start_utc
        first_non_booker_job_start = first_non_booker_job.start_utc

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert last_booker_job_start < booking_end_timestamp
        if last_booker_job.actual_duration < booking.total_duration:
            assert first_non_booker_job_start < booking_end_timestamp


@pytest.mark.skipif(
    not HAS_QUANTIFY, reason="real simulator execution is hard to estimate precisely"
)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_in_idle_booking_before_another(
    client,
    worker,
    redis_conn,
    job,
    jobs_folder,
    mocker,
    fake_clock,
):
    """When there is an idle booking and another is next, only non booker jobs, short enough to fit in between, run."""
    with client as client:
        # create many users and their tokens
        users = create_many_users(client)

        durations = [0.2, 3, 4, 1, 2.9, 0.3, 0.15]
        raw_jobs = get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker = users[0]
        booker_id = booker["id"]

        # create first booking
        first_booking_info = {"duration": 2, "starts_in": 0}
        _, result = create_booking(
            client, user_id=booker_id, booking=first_booking_info
        )

        # create second booking
        second_booking_info = {"duration": 3, "starts_in": 3}
        _, response = create_booking(
            client, user_id=booker_id, booking=second_booking_info
        )
        next_slot = Booking.model_validate(response.json())

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        mss_update_spy = mocker.spy(scheduler_tasks, "update_job_in_mss")
        wait_on_rq_worker(worker, with_scheduler=True, clock=fake_clock)

        jobs_in_db = get_jobs_in_redis(redis_conn)

        booker_jobs = [v for v in jobs_in_db if v.user_id == booker_id]
        non_booker_jobs = [v for v in jobs_in_db if v.user_id != booker_id]

        booker_jobs.sort(key=lambda v: v.estimated_duration)
        non_booker_jobs.sort(key=lambda v: v.start_utc)
        # Note: Enqueue_at ignores microseconds
        non_booker_job_ids_pre_2nd_slot = [
            v.job_id for v in non_booker_jobs if v.start_utc < next_slot.start_utc
        ]
        non_booker_job_ids_post_2nd_slot = [
            v.job_id for v in non_booker_jobs if v.start_utc > next_slot.start_utc
        ]

        # First booker job takes 0.2, waits for 1 second, releases the waitlist.
        # Time left = 3 - (0.2 + 1) = 1.8
        # Therefore job (duration=3) is pushed to waitlist
        # job (duration=4) is pushed to waitlist
        # job (duration=1) is run immediately; time left is about 0.8
        # job (duration=2.9) from booker fails immediately as it is too long for the current booking
        # job (duration=0.3) runs, time left is about 0.5
        # job (duration=0.15) runs, time left is about 0.35
        #
        # first and fifth jobs are submitted by the owner of the booking;
        # the fifth is too long for the current booking so it fails immediately.
        non_booker_pre_2dn_slot_durations = (1, 0.3, 0.15)
        booker_job_durations = (0.2, 2.9)
        expected_non_booker_job_ids_pre_2nd_slot = [
            v["job_id"]
            for v in raw_jobs
            if v["params"]["qobj"]["header"]["test_duration"]
            in non_booker_pre_2dn_slot_durations
        ]
        expected_non_booker_job_ids_post_2nd_slot = [
            v["job_id"]
            for v in raw_jobs
            if v["params"]["qobj"]["header"]["test_duration"]
            not in (*non_booker_pre_2dn_slot_durations, *booker_job_durations)
        ]

        # first job belongs to booker; short enough.
        assert booker_jobs[0].status == JobStatus.SUCCESSFUL
        # fifth job too long for current booking; fails immediately
        assert booker_jobs[1].status == JobStatus.FAILED
        assert (
            "job too long for the time left in the booking"
            in booker_jobs[1].failure_reason
        )
        jobs_sent_to_mss = get_jobs_sent_to_mss(mss_update_spy)
        assert (booker_jobs[1].job_id, JobStatus.FAILED) in [
            (sent_job.job_id, sent_job.status) for sent_job in jobs_sent_to_mss
        ]
        # non booker jobs short enough to finish before next booking (after waiting for 1 second timeout on booked queue)
        assert (
            non_booker_job_ids_pre_2nd_slot == expected_non_booker_job_ids_pre_2nd_slot
        )
        # non booker jobs too long to finish before next booking run after the new booking.
        assert (
            non_booker_job_ids_post_2nd_slot
            == expected_non_booker_job_ids_post_2nd_slot
        )


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_booker_job_longer_than_the_time_left_fails(
    client, worker, redis_conn, job, jobs_folder, mocker, fake_clock
):
    """A booker's job fits the time left in the booking, not the booking's whole length"""
    with client as client:
        [booker] = create_many_users(client, USERS[:1])
        create_booking(
            client, user_id=booker["id"], booking={"duration": 3, "starts_in": 0}
        )
        job_info = get_job_submission_metadata(
            client,
            jobs=get_raw_jobs(job, durations=[1]),
            users=[booker],
            mocker=mocker,
            jobs_folder=jobs_folder,
        )[0]
        submit_jobs(client, data=[job_info])

        fake_clock.shift(2.5)
        worker.work(burst=True, max_jobs=1)

        job_in_db = get_jobs_in_redis(redis_conn)[0]
        assert job_in_db.status == JobStatus.FAILED
        assert (
            job_in_db.failure_reason == "job too long for the time left in the booking"
        )


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_job_whose_booking_ended_in_preprocessing_runs(
    client, worker, redis_conn, job, jobs_folder, mocker, fake_clock
):
    """A job submitted during a booking that ends before it is preprocessed is not
    left on a waitlist that nothing will release"""
    with client as client:
        [booker, other_user] = create_many_users(client, USERS[:2])
        create_booking(
            client, user_id=booker["id"], booking={"duration": 2, "starts_in": 0}
        )
        job_info = get_job_submission_metadata(
            client,
            jobs=get_raw_jobs(job, durations=[0.2]),
            users=[other_user],
            mocker=mocker,
            jobs_folder=jobs_folder,
        )[0]
        submit_jobs(client, data=[job_info])

        fake_clock.shift(3)
        # the booking's end runs before the preprocessing, as the general queue is first
        worker.work(burst=True, with_scheduler=True)

        assert get_jobs_in_redis(redis_conn)[0].status == JobStatus.SUCCESSFUL


@pytest.mark.skipif(
    not HAS_QUANTIFY, reason="real simulator execution is hard to estimate precisely"
)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_long_jobs_before_booking(
    client,
    worker,
    redis_conn,
    job,
    jobs_folder,
    mocker,
    fake_clock,
):
    """POST long jobs to '/jobs' before booking starts waitlists them but runs the shorter ones"""
    with client as client:
        # create many users and their tokens
        users = create_many_users(client)

        durations = [0.3, 3, 4, 0.9, 2.9, 0.2, 0.1]
        raw_jobs = get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker = users[0]
        booker_id = booker["id"]

        # create booking
        booking_info = {"duration": 3, "starts_in": 2.2}
        create_booking(client, user_id=booker_id, booking=booking_info)

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        wait_on_rq_worker(worker, with_scheduler=True, clock=fake_clock)

        jobs_in_db = get_jobs_in_redis(redis_conn)
        jobs_in_db.sort(key=lambda v: v.start_utc)
        job_estimated_durations = [v.estimated_duration for v in jobs_in_db]

        assert all([job.status == JobStatus.SUCCESSFUL for job in jobs_in_db])
        # the small-enough first (0.3, 0.9, 0.2, 0.1),
        # then the bigger ones (3.0, 4.0, 2.9) in original order
        assert job_estimated_durations == [0.3, 0.9, 0.2, 0.1, 3.0, 4.0, 2.9]


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_invalid_token(client, redis_conn, worker, job, jobs_folder):
    """POST '/jobs' with invalid tokens results in an error"""
    with client as client:
        headers = get_bearer_headers("token")
        job_file_path = save_job_file(folder=jobs_folder, job=job)
        with open(job_file_path, "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=headers
            )

        json_response = response.json()

        assert response.status_code == 401
        assert "not authenticated" in json_response["detail"]


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_without_token(client, redis_conn, worker, job, jobs_folder):
    """POST to '/jobs' jobs without a token results in an error"""
    with client as client:
        job_file_path = save_job_file(folder=jobs_folder, job=job)
        with open(job_file_path, "rb") as file:
            response = client.post("/jobs", files={"upload_file": file})

        json_response = response.json()

        assert response.status_code == 401
        assert json_response["detail"] == "user not authenticated"


@pytest.mark.parametrize(
    "client, redis_conn, worker, job", _ALL_INVALID_UPLOAD_JOB_PARAMS
)
def test_upload_invalid_job(
    client, redis_conn, worker, job, jobs_folder, mocker, fake_clock
):
    """POSTing an invalid structure of a job to '/jobs' fails"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata[0]

        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        got = response.json()

        assert response.status_code == 400
        assert got["detail"].startswith("Invalid file: ")

        worker.work(burst=True)
        jobs_in_db = get_jobs_in_redis(redis_conn)
        assert jobs_in_db == []


@pytest.mark.timeout(60)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_duplicate_job_upload(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """Uploading two jobs of the same job_id returns an error"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata[0]

        with open(job_info["file_path"], "rb") as file:
            first_response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

            worker.work(burst=True)

            second_response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

            login_details = {
                "user_id": job_info["user_id"],
                "job_id": job_info["job_id"],
            }
            token, _ = get_token(client, data=login_details)
            fresh_headers = get_bearer_headers(token)
            third_response = client.post(
                "/jobs", files={"upload_file": file}, headers=fresh_headers
            )

    expected_err_resp = {"detail": f"job {job_info["job_id"]} already exists"}
    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json() == expected_err_resp
    assert third_response.status_code == 409
    assert third_response.json() == expected_err_resp


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_remove_job(client, redis_conn, jobs_folder, worker, job, mocker, fake_clock):
    """DELETE to '/jobs/{job_id}' deletes the given job if job belongs to user"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2, 1, 0.3])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # start the worker
        worker.work(burst=True)

        # initiate delete
        job_info = job_metadata_list[0]
        headers = create_mss_headers(user_id=job_info["user_id"])
        deletion_response = client.delete(
            f"/jobs/{job_info["job_id"]}", headers=headers
        )
        # run the rest of the tasks
        worker.work(burst=True)

        jobs_in_db = get_jobs_in_redis(redis_conn)
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list[1:]])
        assert deletion_response.status_code == 200
        assert job_ids_in_db == expected_ids


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_remove_another_users_job(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """DELETE to '/jobs/{job_id}' fails if the user does not own the given job"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2, 1, 0.3])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # start the worker
        worker.work(burst=True)

        # initiate delete
        another_user = users[1]
        job_info = job_metadata_list[0]
        job_id = job_info["job_id"]
        headers = create_mss_headers(user_id=another_user["id"])
        deletion_response = client.delete(f"/jobs/{job_id}", headers=headers)
        # run the rest of the tasks
        worker.work(burst=True)

        jobs_in_db = get_jobs_in_redis(redis_conn)
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list])
        assert deletion_response.status_code == 404
        assert deletion_response.json() == {"detail": f"Job {job_id} not found"}
        assert job_ids_in_db == expected_ids


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_admin_remove_job(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """DELETE to '/jobs/{job_id}' deletes the given job if user is admin"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2, 1, 0.3])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # start the job registration but stop there
        worker.work(burst=True)

        # initiate delete
        another_user = users[1]
        job_info = job_metadata_list[0]
        headers = create_mss_headers(user_id=another_user["id"], is_admin=True)
        deletion_response = client.delete(
            f"/jobs/{job_info["job_id"]}", headers=headers
        )
        # run the rest of the tasks
        worker.work(burst=True)

        jobs_in_db = get_jobs_in_redis(redis_conn)
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list[1:]])
        assert deletion_response.status_code == 200
        assert job_ids_in_db == expected_ids


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_remove_job(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """Delete to /jobs/{job_id} returns 401 error when accessed outside MSS or without proper MSS headers"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2, 1, 0.3])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # start the job registration but stop there
        worker.work(burst=True)

        # initiate delete
        job_info = job_metadata_list[0]
        job_id = job_info["job_id"]

        response = client.delete(f"/jobs/{job_id}")
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        # use a token used only for submitting jobs directly to the backend
        response = client.get(f"/jobs/{job_id}", headers=job_info["headers"])
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        # run the rest of the tasks
        worker.work(burst=True)

        jobs_in_db = get_jobs_in_redis(redis_conn)
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list])
        assert job_ids_in_db == expected_ids


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_future_booking(
    client, worker, redis_conn, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    """POST '/booking/{id}/cancel' for a future booking, deletes the booking and allows jobs to run without it."""
    with client as client:
        users = create_many_users(client)

        # create booking
        # third user; thus third job (duration: 2.1) belongs to them
        booker = users[2]
        booker_id = booker["id"]

        booking_info = {"duration": 3, "starts_in": 2}
        _, response = create_booking(client, user_id=booker_id, booking=booking_info)
        booking = Booking.model_validate(response.json())
        assert response.status_code == 200

        response = cancel_booking(client, user_id=booker_id, booking_id=booking.id)
        expected = {
            "status": "success",
            "detail": f"Booking of id {booking.id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected

        fake_clock.shift(booking_info["starts_in"])
        # Booking does not exist or work
        # submit many jobs from many users when booking starts
        raw_jobs = get_raw_jobs(job, durations=[0.23, 1, 0.4, 0.5])
        job_metadata_list = get_job_submission_metadata(
            client, users=users, jobs=raw_jobs, jobs_folder=jobs_folder, mocker=mocker
        )
        expected_job_ids = submit_jobs(client, data=job_metadata_list)

        # Run the queue
        worker.work(burst=True)

        jobs_in_redis = get_jobs_in_redis(redis_conn)

        jobs_in_redis.sort(key=lambda v: v.start_utc)
        job_ids = [job.job_id for job in jobs_in_redis]

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert job_ids == expected_job_ids


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_future_booking_new_mss_user(
    client, worker, redis_conn, job, jobs_folder, mocker: MockerFixture
):
    """POST '/booking/{id}/cancel' for a future booking, deletes the booking even if it was created without a preexisting user."""
    with client as client:
        booker_id = f"{uuid4()}"

        booking_info = {"duration": 3, "starts_in": 2}
        _, response = create_booking(client, user_id=booker_id, booking=booking_info)
        booking = Booking.model_validate(response.json())
        assert response.status_code == 200

        response = cancel_booking(client, user_id=booker_id, booking_id=booking.id)
        expected = {
            "status": "success",
            "detail": f"Booking of id {booking.id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_admin_cancel_future_booking(client):
    """Admin POST '/bookings/{id}/cancel' cancels any user's future booking"""
    with client as client:
        [user_1, user_2] = create_many_users(client, raw_users=USERS[:2])

        user_1_id = user_1["id"]
        user_2_id = user_2["id"]

        # create booking
        booking_info = {"duration": 3, "starts_in": 2}
        _, response = create_booking(client, user_id=user_1_id, booking=booking_info)
        booking = Booking.model_validate(response.json())

        # Non-admins fail
        response = cancel_booking(client, user_id=user_2_id, booking_id=booking.id)
        json_response = response.json()
        assert response.status_code == 404
        assert "not found" in json_response["detail"]

        response = cancel_booking(
            client, user_id=user_2_id, booking_id=booking.id, is_admin=True
        )
        expected = {
            "status": "success",
            "detail": f"Booking of id {booking.id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_booking_may_start_as_another_ends(client):
    with client as client:
        user_id, _ = create_user(client, user=USERS[0])
        start_utc = datetime.now(timezone.utc) + timedelta(days=1)
        end_utc = start_utc + timedelta(minutes=10)

        first = post_booking(client, user_id, start_utc=start_utc, end_utc=end_utc)
        second = post_booking(
            client, user_id, start_utc=end_utc, end_utc=end_utc + timedelta(minutes=10)
        )

        assert (first.status_code, second.status_code) == (200, 200)


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_booking_across_midnight_counts_toward_both_days(client):
    """MAX_SLOTS_PER_DAY counts the user's bookings that overlap the day a booking starts"""
    with client as client:
        user_id, _ = create_user(client, user=USERS[0])
        midnight = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        crossing = post_booking(
            client,
            user_id,
            start_utc=midnight - timedelta(minutes=5),
            end_utc=midnight + timedelta(minutes=5),
        )
        second = post_booking(
            client,
            user_id,
            start_utc=midnight + timedelta(hours=1),
            end_utc=midnight + timedelta(hours=1, minutes=10),
        )
        third = post_booking(
            client,
            user_id,
            start_utc=midnight + timedelta(hours=2),
            end_utc=midnight + timedelta(hours=2, minutes=10),
        )

        assert TEST_MAX_SLOTS_PER_DAY == 2
        assert crossing.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 400


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_max_slots_per_day_is_per_user(client):
    with client as client:
        [first_user, second_user] = create_many_users(client, raw_users=USERS[:2])
        day = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=3)
        first_user_codes = [
            post_booking(
                client,
                first_user["id"],
                start_utc=day + timedelta(hours=hour),
                end_utc=day + timedelta(hours=hour, minutes=10),
            ).status_code
            for hour in range(TEST_MAX_SLOTS_PER_DAY)
        ]

        second_user_response = post_booking(
            client,
            second_user["id"],
            start_utc=day + timedelta(hours=20),
            end_utc=day + timedelta(hours=20, minutes=10),
        )

        assert first_user_codes == [200] * TEST_MAX_SLOTS_PER_DAY
        assert second_user_response.status_code == 200


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_bookings_are_validated_against_bcc_current_date(client, monkeypatch):
    """BCC_CURRENT_DATE stands in for now, so a time before it is in the past"""
    import settings

    current_date = datetime(2025, 10, 1, tzinfo=timezone.utc)
    with client as client:
        device_settings = settings.get_device_settings(os.environ["BCC_DEFAULT_PREFIX"])
        monkeypatch.setattr(device_settings, "CURRENT_DATE", current_date)
        user_id, _ = create_user(client, user=USERS[0])

        after = post_booking(
            client,
            user_id,
            start_utc=current_date + timedelta(hours=1),
            end_utc=current_date + timedelta(hours=1, minutes=10),
        )
        before = post_booking(
            client,
            user_id,
            start_utc=current_date - timedelta(hours=1),
            end_utc=current_date - timedelta(minutes=50),
        )

        assert (after.status_code, before.status_code) == (200, 422)


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_mss_admin_new_to_the_device_cancels_booking(client):
    """An MSS admin cancels a booking before the device has heard of them"""
    with client as client:
        [user] = create_many_users(client, raw_users=USERS[:1])
        booking_info = {"duration": 3, "starts_in": 2}
        _, response = create_booking(client, user_id=user["id"], booking=booking_info)
        booking = Booking.model_validate(response.json())

        response = cancel_booking(
            client, user_id=f"{uuid4()}", booking_id=booking.id, is_admin=True
        )

        assert response.status_code == 200


@pytest.mark.parametrize(
    "client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS[:-1]
)
def test_cancel_active_booking(
    client, worker, redis_conn, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    """POST '/bookings/{id}/cancel' for an active booking fails."""
    with client as client:
        users = create_many_users(client)

        # create booking
        # third user; thus third job (duration: 2.1) belongs to them
        booker = users[2]
        booker_user_id = booker["id"]
        # duration: 3; max idle time = 1
        booking_info = {"duration": 3, "starts_in": 0}
        _, response = create_booking(
            client, user_id=booker_user_id, booking=booking_info
        )
        booking = Booking.model_validate(response.json())

        response = cancel_booking(client, user_id=booker_user_id, booking_id=booking.id)
        expected = {"detail": f"the booking of id {booking.id} is already active"}
        got = response.json()

        assert response.status_code == 400
        assert got == expected

        # It still works
        # submit many jobs from many users when booking starts
        raw_jobs = get_raw_jobs(job, durations=[0.23, 0.3, 0.1])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, jobs_folder=jobs_folder, mocker=mocker
        )
        submit_jobs(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        wait_on_rq_worker(worker, with_scheduler=True, clock=fake_clock)

        jobs_in_redis = get_jobs_in_redis(redis_conn)

        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        booker_jobs = [job for job in jobs_in_redis if job.user_id == booker_user_id]
        last_booker_job = booker_jobs[-1]

        # Assert that they are all complete and their booker jobs started before booking end_utc
        # while non booker jobs started after end_utc
        # Note: Enqueue_at ignores microseconds
        booking_end_timestamp = booking.end_utc
        last_booker_job_start = last_booker_job.timestamps.execution.start_timestamp

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert last_booker_job_start < booking_end_timestamp


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_completed_booking(
    client, worker, redis_conn, job, mocker: MockerFixture, fake_clock
):
    """POST '/bookings/{id}/cancel' for a completed booking fails."""
    with client as client:
        users = create_many_users(client)

        # create booking
        booker = users[0]
        booker_user_id = booker["id"]
        # duration: 2; max idle time = 1
        booking_info = {"duration": 2, "starts_in": 0}
        _, response = create_booking(
            client, user_id=booker_user_id, booking=booking_info
        )
        booking = Booking.model_validate(response.json())

        fake_clock.shift(2)

        response = cancel_booking(client, user_id=booker_user_id, booking_id=booking.id)
        expected = {"detail": f"the booking of id {booking.id} is already complete"}
        got = response.json()

        assert response.status_code == 400
        assert got == expected


@pytest.mark.parametrize("client, worker, job, device_name, user", _VIEW_JOB_PARAMS)
def test_view_job(
    client,
    worker,
    job,
    device_name,
    user,
    jobs_folder,
    mocker: MockerFixture,
    redis_conn_spy,
    fake_clock,
):
    """GET '/jobs/{job_id}' by a user can show the job for the job_id if job belongs to them"""
    job_file_path = save_job_file(folder=jobs_folder, job=job)

    with client as client:
        user_id, _ = create_user(client, user=user)
        other_user_id, _ = create_user(
            client, user={**user, "email": f"extra-{user["email"]}"}
        )
        job_id = job["job_id"]
        token, _ = get_token(client, data={"user_id": user_id, "job_id": job_id})

        # submit job
        headers = get_bearer_headers(token)

        with open(job_file_path, "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=headers
            )

        expected_job = Job.model_validate(response.json())

        pending_job, _ = view_job(client, user_id=user_id, job_id=job_id)
        assert pending_job == expected_job

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True)

        complete_job, _ = view_job(client, user_id=user_id, job_id=job_id)
        expected_completed_job = Job(
            job_id=job_id,
            device=device_name,
            calibration_date=complete_job.calibration_date,
            updated_at=complete_job.updated_at,
            created_at=complete_job.created_at,
            user_id=user_id,
            stage=Stage.FINAL_W,
            status=JobStatus.SUCCESSFUL,
            result=complete_job.result,
            timestamps=complete_job.timestamps,
            estimated_duration=complete_job.estimated_duration,
            actual_duration=complete_job.actual_duration,
            download_url=complete_job.download_url,
            storage_id=f"{job_id}:::{complete_job.estimated_duration}",
        )
        assert complete_job == expected_completed_job
        assert redis_conn_spy.call_count <= 1


@pytest.mark.parametrize("client, worker, job, device_name, user", _VIEW_JOB_PARAMS)
def test_unauthenticated_view_job(
    client, worker, job, device_name, user, jobs_folder, mocker: MockerFixture
):
    """Get to /jobs/{job_id} raise 401 if not accessed through MSS"""
    job_file_path = save_job_file(folder=jobs_folder, job=job)

    with client as client:
        user_id, _ = create_user(client, user=user)
        job_id = job["job_id"]
        token, _ = get_token(client, data={"user_id": user_id, "job_id": job_id})

        # submit job
        headers = get_bearer_headers(token)

        with open(job_file_path, "rb") as file:
            client.post("/jobs", files={"upload_file": file}, headers=headers)

        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        # use a token used only for submitting jobs directly to the backend
        response = client.get(f"/jobs/{job_id}", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.parametrize("client, worker, job, device_name, user", _VIEW_JOB_PARAMS[:3])
def test_unauthorized_view_job(
    client, worker, job, device_name, user, jobs_folder, mocker: MockerFixture
):
    """Get to /jobs/{job_id} raise 404 if user is not owner"""
    job_file_path = save_job_file(folder=jobs_folder, job=job)

    with client as client:
        user_id, _ = create_user(client, user=user)
        other_user_id, _ = create_user(
            client, user={**user, "email": f"extra-{user["email"]}"}
        )
        job_id = job["job_id"]
        token, _ = get_token(client, data={"user_id": user_id, "job_id": job_id})

        # submit job
        headers = get_bearer_headers(token)

        with open(job_file_path, "rb") as file:
            client.post("/jobs", files={"upload_file": file}, headers=headers)

        received_job, response = view_job(client, user_id=other_user_id, job_id=job_id)
        assert received_job is None

        assert response.status_code == 404
        assert response.json() == {"detail": f"Job {job_id} not found"}


@pytest.mark.parametrize("client, worker, job, device_name, user", _VIEW_JOB_PARAMS[:3])
def test_admin_view_job(
    client, worker, job, device_name, user, jobs_folder, mocker: MockerFixture
):
    """Get to /jobs/{job_id} returns the job if user is admin"""
    job_file_path = save_job_file(folder=jobs_folder, job=job)

    with client as client:
        user_id, _ = create_user(client, user=user)
        other_user_id, _ = create_user(
            client, user={**user, "email": f"extra-{user["email"]}"}
        )
        job_id = job["job_id"]
        token, _ = get_token(client, data={"user_id": user_id, "job_id": job_id})

        # submit job
        headers = get_bearer_headers(token)

        with open(job_file_path, "rb") as file:
            resp = client.post("/jobs", files={"upload_file": file}, headers=headers)

        expected_job = Job.model_validate(resp.json())

        received_job, response = view_job(
            client, user_id=other_user_id, job_id=job_id, is_admin=True
        )

        assert response.status_code == 200
        assert received_job == expected_job


@pytest.mark.skipif(not HAS_QUANTIFY, reason="real simulator takes too long")
@pytest.mark.parametrize("client, _redis, worker, job, device", _VIEW_JOBS_PARAMS)
def test_view_jobs(
    client, _redis, worker, job, device, jobs_folder, mocker: MockerFixture, fake_clock
):
    """GET '/jobs' by a user can show the paginated list of jobs that belong to them"""
    with client as client:
        # create many users and their tokens
        users = create_many_users(client)
        user_count = len(users)

        # current user
        curr_user = users[0]
        user_id = curr_user["id"]

        durations = [0.4, 3, 4, 1.1, 2.9, 0.3, 0.1]
        raw_jobs = []
        for duration in durations:
            new_job = {**job, "job_id": f"{uuid4()}"}
            new_job["params"]["qobj"]["header"]["test_duration"] = duration
            raw_jobs.append(new_job)

        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        job_ids = submit_jobs(client, data=job_metadata_list)

        # get jobs
        headers = create_mss_headers(user_id)
        response = client.get("/jobs", headers=headers)
        pending_jobs_resp = response.json()
        jobs_in_resp = {v["job_id"]: v for v in pending_jobs_resp["data"]}

        expected_jobs = [
            Job(
                job_id=job_id,
                device=device,
                calibration_date=jobs_in_resp[job_id]["calibration_date"],
                updated_at=jobs_in_resp[job_id]["updated_at"],
                created_at=jobs_in_resp[job_id]["created_at"],
                user_id=user_id,
                stage=Stage.PRE_PROC_Q,
                status=JobStatus.PENDING,
            ).model_dump(mode="json")
            for idx, job_id in enumerate(job_ids)
            if idx % user_count == 0
        ]

        # ensure ordering is the same
        pending_jobs_resp["data"] = order_by(pending_jobs_resp["data"], field="job_id")
        expected_jobs = order_by(expected_jobs, field="job_id")

        assert pending_jobs_resp == paginate(expected_jobs)

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True)

        # refresh the headers
        headers = create_mss_headers(user_id)
        response = client.get("/jobs", headers=headers)
        completed_jobs_resp = response.json()
        jobs_in_resp = {v["job_id"]: v for v in completed_jobs_resp["data"]}

        expected_jobs = [
            {
                **v,
                "status": JobStatus.SUCCESSFUL.value,
                "stage": Stage.FINAL_W.value,
                "updated_at": jobs_in_resp[v["job_id"]]["updated_at"],
                "result": jobs_in_resp[v["job_id"]]["result"],
                "timestamps": jobs_in_resp[v["job_id"]]["timestamps"],
                "estimated_duration": jobs_in_resp[v["job_id"]]["estimated_duration"],
                "actual_duration": jobs_in_resp[v["job_id"]]["actual_duration"],
                "download_url": jobs_in_resp[v["job_id"]]["download_url"],
                "storage_id": f"{v['job_id']}:::{jobs_in_resp[v["job_id"]]["estimated_duration"]}",
            }
            for idx, v in enumerate(expected_jobs)
        ]

        # ensure ordering is the same
        completed_jobs_resp["data"] = order_by(
            completed_jobs_resp["data"], field="job_id"
        )
        assert completed_jobs_resp == paginate(expected_jobs)


@pytest.mark.skipif(not HAS_QUANTIFY, reason="real simulator takes too long")
@pytest.mark.parametrize("client, _redis, worker, job, device", _VIEW_JOBS_PARAMS)
def test_view_jobs_by_status(
    client, _redis, worker, job, device, jobs_folder, mocker: MockerFixture, fake_clock
):
    """GET '/jobs' by a user can show the paginated list of jobs that belong to them"""
    with client as client:
        # create many users and their tokens
        users = create_many_users(client)
        user_count = len(users)

        # current user
        curr_user = users[0]
        user_id = curr_user["id"]

        durations = [0.4, 3, 4, 1.1, 2.9, 0.3, 0.1]
        raw_jobs = []
        for duration in durations:
            new_job = {**job, "job_id": f"{uuid4()}"}
            new_job["params"]["qobj"]["header"]["test_duration"] = duration
            raw_jobs.append(new_job)

        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        completed_job_ids = submit_jobs(client, data=job_metadata_list)

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True)

        new_raw_jobs = [{**v, "job_id": f"{uuid4()}"} for v in raw_jobs]
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client,
            jobs=new_raw_jobs,
            users=users,
            mocker=mocker,
            jobs_folder=jobs_folder,
        )

        # submit many jobs from many users when booking starts
        pending_job_ids = submit_jobs(client, data=job_metadata_list)

        # get pending jobs
        headers = create_mss_headers(user_id)
        response = client.get(
            "/jobs", headers=headers, params={"status": JobStatus.PENDING.value}
        )
        pending_jobs_resp = response.json()
        jobs_in_resp = {v["job_id"]: v for v in pending_jobs_resp["data"]}

        expected_pending_jobs = [
            Job(
                job_id=job_id,
                device=device,
                calibration_date=jobs_in_resp[job_id]["calibration_date"],
                updated_at=jobs_in_resp[job_id]["updated_at"],
                created_at=jobs_in_resp[job_id]["created_at"],
                user_id=user_id,
                stage=Stage.PRE_PROC_Q,
                status=JobStatus.PENDING,
            ).model_dump(mode="json")
            for idx, job_id in enumerate(pending_job_ids)
            if idx % user_count == 0
        ]

        # ensure ordering is the same
        pending_jobs_resp["data"] = order_by(pending_jobs_resp["data"], field="job_id")
        expected_pending_jobs = order_by(expected_pending_jobs, field="job_id")

        assert pending_jobs_resp == paginate(expected_pending_jobs)

        ## Get completed jobs

        # refresh the headers
        headers = create_mss_headers(user_id)
        response = client.get(
            "/jobs", headers=headers, params={"status": JobStatus.SUCCESSFUL.value}
        )
        completed_jobs_resp = response.json()
        jobs_in_resp = {v["job_id"]: v for v in completed_jobs_resp["data"]}

        expected_completed_jobs = [
            Job(
                job_id=job_id,
                device=device,
                calibration_date=jobs_in_resp[job_id]["calibration_date"],
                updated_at=jobs_in_resp[job_id]["updated_at"],
                created_at=jobs_in_resp[job_id]["created_at"],
                user_id=user_id,
                stage=Stage.FINAL_W,
                status=JobStatus.SUCCESSFUL,
                result=jobs_in_resp[job_id]["result"],
                timestamps=Timestamps(**jobs_in_resp[job_id]["timestamps"]),
                estimated_duration=jobs_in_resp[job_id]["estimated_duration"],
                actual_duration=jobs_in_resp[job_id]["actual_duration"],
                download_url=jobs_in_resp[job_id]["download_url"],
                storage_id=f"{job_id}:::{jobs_in_resp[job_id]["estimated_duration"]}",
            ).model_dump(mode="json")
            for idx, job_id in enumerate(completed_job_ids)
            if idx % user_count == 0
        ]

        # ensure ordering is the same
        completed_jobs_resp["data"] = order_by(
            completed_jobs_resp["data"], field="job_id"
        )
        expected_completed_jobs = order_by(expected_completed_jobs, field="job_id")
        assert completed_jobs_resp == paginate(expected_completed_jobs)

        ## Get all jobs

        # refresh the headers
        headers = create_mss_headers(user_id)
        response = client.get("/jobs", headers=headers)
        all_jobs_resp = response.json()

        expected_all_jobs = expected_completed_jobs + expected_pending_jobs
        expected_all_jobs = order_by(expected_all_jobs, field="job_id")

        # ensure ordering is the same
        all_jobs_resp["data"] = order_by(all_jobs_resp["data"], field="job_id")
        assert all_jobs_resp == paginate(expected_all_jobs)


@pytest.mark.parametrize("client, _redis, worker, job, device", _VIEW_JOBS_PARAMS)
def test_unauthenticated_view_jobs(
    client, _redis, worker, job, device, jobs_folder, mocker: MockerFixture
):
    """Get to /jobs/ raise 401 if not accessed through MSS"""
    with client as client:
        # create many users and their tokens
        users = create_many_users(client)

        # current user
        curr_user = users[0]
        user_id = curr_user["id"]

        durations = [0.4, 3, 4, 1.1, 2.9, 0.3, 0.1]
        raw_jobs = []
        for duration in durations:
            new_job = {**job, "job_id": f"{uuid4()}"}
            new_job["params"]["qobj"]["header"]["test_duration"] = duration
            raw_jobs.append(new_job)

        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        job_ids = submit_jobs(client, data=job_metadata_list)

        response = client.get("/jobs")
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        # use a token used only for submitting jobs directly to the backend
        token, _ = get_token(client, data={"user_id": user_id, "job_id": job_ids[0]})
        headers = get_bearer_headers(token)
        response = client.get("/jobs", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_job_via_mss(
    client, redis_conn, jobs_folder, worker, job, mocker, redis_conn_spy, fake_clock
):
    """An MSS POST to '/jobs/{id}/cancel' cancels the job of the job_id if the job belongs to the current user"""
    with client as client:
        cancellation_reason = "just testing"

        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]
        job_id = job_info["job_id"]
        user_id = job_info["user_id"]

        # submit many jobs from many users
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        original_job = response.json()

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True, max_jobs=1)

        response = cancel_job_via_mss(
            client, user_id=user_id, job_id=job_id, reason=cancellation_reason
        )
        expected = {
            "status": "success",
            "detail": f"Job of id {job_id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected

        job_in_db = get_jobs_in_redis(redis_conn)[0]
        preprocess_start = job_in_db.timestamps.pre_processing.started
        preprocess_end = job_in_db.timestamps.pre_processing.finished

        expected_job = Job(
            **{
                **original_job,
                "status": JobStatus.CANCELLED.value,
                "cancellation_reason": cancellation_reason,
                "estimated_duration": 0.2,
                "stage": 4,
                "timestamps": {
                    "registration": None,
                    "pre_processing": {
                        "started": preprocess_start,
                        "finished": preprocess_end,
                    },
                    "execution": None,
                    "post_processing": None,
                    "final": None,
                },
                "created_at": job_in_db.created_at,
                "updated_at": job_in_db.updated_at,
            }
        )
        assert response.status_code == 200
        assert job_in_db == expected_job
        assert redis_conn_spy.call_count <= 1


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_job_directly(
    client, redis_conn, jobs_folder, worker, job, mocker, redis_conn_spy, fake_clock
):
    """A POST to '/jobs/{id}/cancel' with JWT token cancels the job of the job_id if the job belongs to user"""
    with client as client:
        cancellation_reason = "just testing"

        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]
        job_id = job_info["job_id"]

        # submit many jobs from many users
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        original_job = response.json()

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True, max_jobs=1)

        response = client.post(
            f"/jobs/{job_id}/cancel",
            json={"reason": cancellation_reason},
            headers=job_info["headers"],
        )
        expected = {
            "status": "success",
            "detail": f"Job of id {job_id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected

        job_in_db = get_jobs_in_redis(redis_conn)[0]
        preprocess_start = job_in_db.timestamps.pre_processing.started
        preprocess_end = job_in_db.timestamps.pre_processing.finished

        expected_job = Job(
            **{
                **original_job,
                "status": JobStatus.CANCELLED.value,
                "cancellation_reason": cancellation_reason,
                "estimated_duration": 0.2,
                "stage": 4,
                "timestamps": {
                    "registration": None,
                    "pre_processing": {
                        "started": preprocess_start,
                        "finished": preprocess_end,
                    },
                    "execution": None,
                    "post_processing": None,
                    "final": None,
                },
                "created_at": job_in_db.created_at,
                "updated_at": job_in_db.updated_at,
            }
        )
        assert response.status_code == 200
        assert job_in_db == expected_job
        assert redis_conn_spy.call_count <= 1


@pytest.mark.parametrize("patch_target, patch_kwargs", _FAILING_STAGE_PATCHES)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_failed_job_is_marked_failed_and_sent_to_mss(
    client,
    redis_conn,
    jobs_folder,
    worker,
    job,
    mocker,
    fake_clock,
    patch_target,
    patch_kwargs,
):
    """A job failing at any stage ends FAILED, and MSS is told"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        mocker.patch(patch_target, **patch_kwargs)
        mss_update_spy = mocker.spy(scheduler_tasks, "update_job_in_mss")
        with open(job_info["file_path"], "rb") as file:
            client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        worker.work(burst=True)

        job_in_db = get_jobs_in_redis(redis_conn)[0]
        assert job_in_db.status == JobStatus.FAILED
        assert get_jobs_sent_to_mss(mss_update_spy)[-1].status == JobStatus.FAILED


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_finished_job_is_sent_to_mss_with_its_results(
    client, redis_conn, jobs_folder, worker, job, mocker, mock_mss, fake_clock
):
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])

        worker.work(burst=True)

        job_updates = [
            event["data"]
            for event in mock_mss.sent_events
            if event["name"] == "job_updated"
        ]
        assert {update["job_id"] for update in job_updates} == {job_info["job_id"]}
        assert job_updates[-1]["status"] == "successful"
        assert job_updates[-1]["result"]["memory"]


@pytest.mark.skipif(not HAS_QUANTIFY, reason="the simulators send no calibration")
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_recalibration_sends_the_calibration_to_mss(
    client, redis_conn, worker, mock_mss
):
    with client as client:
        start_up_event_count = len(mock_mss.sent_events)

        worker.work(burst=True, max_jobs=1)

        [recalibrated] = mock_mss.sent_events[start_up_event_count:]
        assert recalibrated["name"] == "recalibrated"
        assert recalibrated["data"]["qubits"]


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_job_cancels_its_rq_job(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """Cancelling a queued job cancels the rq job that would run it"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        job_id = job_info["job_id"]
        with open(job_info["file_path"], "rb") as file:
            client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )
        worker.work(burst=True, max_jobs=1)

        cancel_job_via_mss(client, user_id=job_info["user_id"], job_id=job_id)

        rq_job = RqJob.fetch(get_rq_job_id(job_id, Stage.EXEC_Q), connection=redis_conn)
        assert rq_job.get_status() == "canceled"


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_mss_admin_new_to_the_device_views_and_cancels_job(
    client, redis_conn, jobs_folder, worker, job, mocker
):
    """An MSS admin views and cancels a job before the device has heard of them"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])
        admin_id = f"{uuid4()}"

        _, view_response = view_job(
            client, user_id=admin_id, job_id=job_info["job_id"], is_admin=True
        )
        cancel_response = cancel_job_via_mss(
            client, user_id=admin_id, job_id=job_info["job_id"], is_admin=True
        )

        assert view_response.status_code == 200
        assert cancel_response.status_code == 200


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_finished_job(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """POST '/jobs/{id}/cancel' leaves a job that has finished as it is"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])
        worker.work(burst=True)

        response = cancel_job_via_mss(
            client, user_id=job_info["user_id"], job_id=job_info["job_id"]
        )

        assert response.status_code == 406
        assert get_jobs_in_redis(redis_conn)[0].status == JobStatus.SUCCESSFUL


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_remove_cancelled_job(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """DELETE '/jobs/{id}' deletes a job that was cancelled"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        user_id, job_id = job_info["user_id"], job_info["job_id"]
        submit_jobs(client, data=[job_info])
        worker.work(burst=True, max_jobs=1)
        cancel_job_via_mss(client, user_id=user_id, job_id=job_id)

        response = client.delete(f"/jobs/{job_id}", headers=create_mss_headers(user_id))

        assert response.status_code == 200
        assert get_jobs_in_redis(redis_conn) == []


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_another_users_job(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """A user POST to '/jobs/{id}/cancel' for a job does is not for the current user errors out"""
    with client as client:
        cancellation_reason = "just testing"

        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]
        job_id = job_info["job_id"]

        # submit many jobs from many users
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )
            original_job = Job.model_validate(response.json())

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True, max_jobs=1)

        other_user_id = users[1]["id"]
        response = cancel_job_via_mss(
            client, user_id=other_user_id, job_id=job_id, reason=cancellation_reason
        )
        expected = {
            "detail": f"Job {job_id} not found",
        }
        got = response.json()

        assert response.status_code == 404
        assert got == expected

        job_in_db = get_jobs_in_redis(redis_conn)[0]

        expected_job = original_job.model_copy(
            update={
                "stage": Stage.EXEC_Q,
                "estimated_duration": job_in_db.estimated_duration,
                "updated_at": job_in_db.updated_at,
                "timestamps": job_in_db.timestamps,
                "storage_id": f"{job_id}:::{job_in_db.estimated_duration}",
            }
        )

        assert job_in_db == expected_job


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_admin_cancel_job(
    client, redis_conn, jobs_folder, worker, job, mocker, fake_clock
):
    """A user POST to '/jobs/{id}/cancel' cancels the job of the job_id if the user is admin"""
    with client as client:
        cancellation_reason = "just testing"

        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]
        job_id = job_info["job_id"]

        # submit many jobs from many users
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True, max_jobs=1)

        original_job = response.json()

        other_user_id = users[1]["id"]
        response = cancel_job_via_mss(
            client,
            user_id=other_user_id,
            job_id=job_id,
            reason=cancellation_reason,
            is_admin=True,
        )
        expected = {
            "status": "success",
            "detail": f"Job of id {job_id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected

        job_in_db = get_jobs_in_redis(redis_conn)[0]
        preprocess_start = job_in_db.timestamps.pre_processing.started
        preprocess_end = job_in_db.timestamps.pre_processing.finished

        expected_job = Job(
            **{
                **original_job,
                "status": JobStatus.CANCELLED.value,
                "cancellation_reason": cancellation_reason,
                "estimated_duration": 0.2,
                "stage": 4,
                "timestamps": {
                    "registration": None,
                    "pre_processing": {
                        "started": preprocess_start,
                        "finished": preprocess_end,
                    },
                    "execution": None,
                    "post_processing": None,
                    "final": None,
                },
                "created_at": job_in_db.created_at,
                "updated_at": job_in_db.updated_at,
            }
        )
        assert response.status_code == 200
        assert job_in_db == expected_job


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_cancel_job(
    client, redis_conn, worker, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    """POST to `/jobs/{job_id}/cancel` raises 401 if not accessed through MSS"""
    with client as client:
        cancellation_reason = "just testing"

        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]
        job_id = job_info["job_id"]

        # submit many jobs from many users
        with open(job_info["file_path"], "rb") as file:
            client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True, max_jobs=1)

        payload = {"reason": cancellation_reason}
        url = f"/jobs/{job_id}/cancel"

        response = client.post(url, json=payload)
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_download_logfile(
    logfile_download_folder, client, redis_conn, worker, job, mocker, jobs_folder
):
    """GET to '/logfiles/{logfile_id}' downloads the given logfile"""
    # using context manager to ensure on_startup runs
    with client as client:
        save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

        users = create_many_users(client, raw_users=USERS[:1])

        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=[job], users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        response = client.get(f"/logfiles/{job["job_id"]}", headers=job_info["headers"])
        file_content = json.loads(response.content)
        assert response.status_code == 200
        assert file_content == job


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_logfile_for_non_existing_job(
    logfile_download_folder, client, redis_conn, worker, job, mocker
):
    """GET '/logfiles/{logfile_id}' for non-existing jobs fails with a 404"""
    # using context manager to ensure on_startup runs
    with client as client:
        job_id = job["job_id"]
        save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

        user_id, _ = create_user(client, USERS[0])
        login_data = {"user_id": user_id, "job_id": job_id}
        token, _ = get_token(client, data=login_data)

        headers = get_bearer_headers(token)

        response = client.get(f"/logfiles/{job_id}", headers=headers)
        assert response.status_code == 404
        assert response.json() == {"detail": f"job {job_id} does not exist"}


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_download_logfile(
    logfile_download_folder, client, redis_conn, worker, job, mocker, jobs_folder
):
    """Unauthenticated GET to '/logfiles/{logfile_id}' errors out"""
    # using context manager to ensure on_startup runs
    with client as client:
        save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

        users = create_many_users(client, raw_users=USERS[:1])

        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = get_job_submission_metadata(
            client, jobs=[job], users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        submit_jobs(client, data=job_metadata_list)

        # no authentication
        response = client.get(f"/logfiles/{job["job_id"]}")
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        # wrong authentication
        headers = get_bearer_headers("foo")
        response = client.get(f"/logfiles/{job["job_id"]}", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "not authenticated"}


@pytest.mark.parametrize("client, expected", _STATIC_PROPERTIES_PARAMS)
def test_get_static_properties(client, expected):
    """Get to '/static-properties' retrieves the current static properties of the backend"""
    # using context manager to ensure on_startup runs
    with client as client:
        user_id, _ = create_user(client, USERS[0])
        headers = create_mss_headers(user_id)

        response = client.get("/static-properties", headers=headers)
        got = response.json()
        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client, _expected", _STATIC_PROPERTIES_PARAMS)
def test_unauthenticated_get_static_props(client, _expected):
    """Get to '/static-properties' outside MSS fails with 401"""
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/static-properties")
        got = response.json()
        assert response.status_code == 401
        assert got == {"detail": "user not authenticated"}


@pytest.mark.parametrize("client, expected", _DYNAMIC_PROPERTIES_PARAMS)
def test_get_dynamic_properties(client, expected):
    """Get to '/dynamic-properties' retrieves the calibrated device parameters"""
    # using context manager to ensure on_startup runs
    with client as client:
        # the heavy worker publishes its executor's calibration, e.g. the simulators'
        # trained discriminators, once the API has started
        preload_worker_globals(should_load_executor=True)
        user_id, _ = create_user(client, USERS[0])
        headers = create_mss_headers(user_id)

        response = client.get("/dynamic-properties", headers=headers)
        got = response.json()
        assert response.status_code == 200
        assert remove_dates(got) == expected


@pytest.mark.parametrize("client, _expected", _STATIC_PROPERTIES_PARAMS)
def test_unauthenticated_get_dynamic_props(client, _expected):
    """Get to '/dynamic-properties' outside MSS fails with 401"""
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/dynamic-properties")
        got = response.json()
        assert response.status_code == 401
        assert got == {"detail": "user not authenticated"}


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_init_recalibration_with_interval(
    client, redis_conn: Redis, worker, quantify_seed_file, fake_clock
):
    """Init recalibration restarts the calibration sequence, changing the interval from the default"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        initial_interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        max_queue_time = TEST_MAX_RECALIBRATION_QUEUE_TIME
        new_interval = 4

        non_admin_headers = create_mss_headers(user_id, is_admin=False)
        admin_headers = create_mss_headers(user_id, is_admin=True)

        initial_rq_job = queue.jobs[-1]
        initial_rq_interval = initial_rq_job.meta.get("interval", None)

        # non admins are not allowed
        response = client.post("/recalibration/init", headers=non_admin_headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}

        before_request_timestamp = datetime.now(timezone.utc)
        # admins are allowed
        response = client.post(
            "/recalibration/init",
            headers=admin_headers,
            params={"interval": new_interval},
        )
        actual_output = response.json()

        assert response.status_code == 200
        assert actual_output == {"status": "success"}

        wait_on_rq_worker(
            worker,
            max_jobs=2,
            max_idle_time=initial_interval + max_queue_time,
            with_scheduler=True,
            queue=queue,
            clock=fake_clock,
        )

        final_rq_job_id = queue.finished_job_registry.get_job_ids(start=-1, end=-1)[0]
        final_rq_job = RqJob.fetch(final_rq_job_id, connection=redis_conn)
        final_rq_interval = final_rq_job.meta.get("interval", None)
        final_actual_interval = (
            final_rq_job.started_at - before_request_timestamp
        ).total_seconds()
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        assert initial_rq_interval == initial_interval
        assert final_rq_interval == new_interval
        assert final_rq_job.is_finished
        assert initial_rq_job.is_canceled
        assert math.isclose(final_actual_interval, new_interval, abs_tol=1)
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_init_recalibration_without_interval(
    client, redis_conn: Redis, worker, quantify_seed_file, fake_clock
):
    """Init recalibration restarts the calibration sequence without interval continues with the default interval"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        initial_interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        max_queue_time = TEST_MAX_RECALIBRATION_QUEUE_TIME
        admin_headers = create_mss_headers(user_id, is_admin=True)
        initial_rq_job = queue.jobs[-1]
        initial_rq_interval = initial_rq_job.meta.get("interval", None)

        before_request_timestamp = datetime.now(timezone.utc)
        # admins are allowed
        response = client.post(
            "/recalibration/init",
            headers=admin_headers,
        )
        actual_output = response.json()

        assert response.status_code == 200
        assert actual_output == {"status": "success"}

        wait_on_rq_worker(
            worker,
            max_jobs=2,
            max_idle_time=initial_interval + max_queue_time,
            with_scheduler=True,
            queue=queue,
            clock=fake_clock,
        )

        final_rq_job_id = queue.finished_job_registry.get_job_ids(start=-1, end=-1)[0]
        final_rq_job = RqJob.fetch(final_rq_job_id, connection=redis_conn)
        final_rq_interval = final_rq_job.meta.get("interval", None)
        final_actual_interval = (
            final_rq_job.started_at - before_request_timestamp
        ).total_seconds()
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        assert initial_rq_interval == initial_interval
        assert final_rq_interval == initial_interval
        assert final_rq_job.is_finished
        assert initial_rq_job.is_canceled
        assert math.isclose(final_actual_interval, initial_interval, abs_tol=1)
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
def test_api_workers_start_a_device_one_at_a_time(quantify_rest_client, mocker):
    from app.api import app, dependencies

    starting, overlaps = [], []

    def start_device():
        starting.append(None)
        overlaps.append(len(starting))
        time.sleep(0.2)
        starting.pop()

    async def start_and_stop():
        async with dependencies.lifespan(app):
            pass

    mocker.patch.object(dependencies, "_start_device", side_effect=start_device)
    # threads share the Redis connections that separate worker processes would not
    mocker.patch.object(dependencies, "clear_redis_connections")
    workers = [
        threading.Thread(target=asyncio.run, args=(start_and_stop(),)) for _ in range(3)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert overlaps == [1, 1, 1]


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_init_recalibration_with_start_time(
    client, redis_conn: Redis, worker, quantify_seed_file, fake_clock
):
    """POST /recalibration/cancel restarts the recalibration sequence at a given start time in future"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        initial_interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        max_queue_time = TEST_MAX_RECALIBRATION_QUEUE_TIME
        headers = create_mss_headers(user_id, is_admin=True)

        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)
        # admins are allowed
        response = client.post(
            "/recalibration/init",
            headers=headers,
            params={"start_timestamp": start_timestamp.isoformat()},
        )
        actual_output = response.json()

        assert response.status_code == 200
        assert actual_output == {"status": "success"}

        wait_on_rq_worker(
            worker,
            max_jobs=1,
            max_idle_time=initial_interval + max_queue_time,
            with_scheduler=True,
            queue=queue,
            clock=fake_clock,
        )

        first_rq_job_id = queue.finished_job_registry.get_job_ids(start=0, end=1)[0]
        first_rq_job = RqJob.fetch(first_rq_job_id, connection=redis_conn)
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        assert first_rq_job.is_finished
        assert math.isclose(
            first_rq_job.started_at.timestamp(), start_timestamp.timestamp(), abs_tol=1
        )
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_get_recalibration_info(
    client, redis_conn: Redis, worker, quantify_seed_file, fake_clock
):
    """Get /recalibration/info gets recalibration info retrieves the basic recalibration info"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        initial_interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        max_queue_time = TEST_MAX_RECALIBRATION_QUEUE_TIME
        new_interval = 4.0

        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/recalibration/info", headers=headers)
        assert response.status_code == 200
        initial_info = response.json()

        headers = create_mss_headers(user_id, is_admin=True)
        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)
        response = client.post(
            "/recalibration/init",
            headers=headers,
            params={
                "start_timestamp": start_timestamp.isoformat(),
                "interval": new_interval,
            },
        )
        assert response.status_code == 200

        # get info after altering recalibration
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/recalibration/info", headers=headers)
        assert response.status_code == 200
        pre_run_info = response.json()

        wait_on_rq_worker(
            worker,
            max_jobs=2,
            max_idle_time=initial_interval + max_queue_time,
            with_scheduler=True,
            queue=queue,
            clock=fake_clock,
        )

        # get info after running the worker
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/recalibration/info", headers=headers)
        assert response.status_code == 200
        post_run_info = response.json()

        cancelled_rq_job_id = queue.canceled_job_registry.get_job_ids(start=0, end=1)[0]
        cancelled_rq_job = RqJob.fetch(cancelled_rq_job_id, connection=redis_conn)

        first_finished_rq_job_id = queue.finished_job_registry.get_job_ids(
            start=0, end=1
        )[0]

        last_finished_rq_job_id = queue.finished_job_registry.get_job_ids(
            start=-1, end=-1
        )[0]
        last_finished_rq_job = RqJob.fetch(
            last_finished_rq_job_id, connection=redis_conn
        )

        next_rq_job_id = queue.scheduled_job_registry.get_job_ids(start=0, end=1)[0]

        def to_datetime_str(v: datetime) -> str:
            return get_timestamp_str(v, timespec="microseconds")

        # asserts of info shown
        assert initial_info == {
            "current_job_id": None,
            "current_job_status": None,
            "current_job_started_at": None,
            "previous_job_id": None,
            "previous_job_status": None,
            "previous_job_started_at": None,
            "previous_job_ended_at": None,
            "previous_job_error": None,
            "next_job_id": cancelled_rq_job_id,
            "next_job_status": "queued",
            "next_job_enqueued_at": to_datetime_str(cancelled_rq_job.enqueued_at),
            "interval": initial_interval,
        }
        assert pre_run_info == {
            "current_job_id": None,
            "current_job_status": None,
            "current_job_started_at": None,
            "previous_job_id": None,
            "previous_job_status": None,
            "previous_job_started_at": None,
            "previous_job_ended_at": None,
            "previous_job_error": None,
            "next_job_id": first_finished_rq_job_id,
            "next_job_status": "scheduled",
            "next_job_enqueued_at": None,
            "interval": new_interval,
        }
        assert post_run_info == {
            "current_job_id": None,
            "current_job_status": None,
            "current_job_started_at": None,
            "previous_job_id": last_finished_rq_job_id,
            "previous_job_status": "finished",
            "previous_job_started_at": to_datetime_str(last_finished_rq_job.started_at),
            "previous_job_ended_at": to_datetime_str(last_finished_rq_job.ended_at),
            "previous_job_error": None,
            "next_job_id": next_rq_job_id,
            "next_job_status": "scheduled",
            "next_job_enqueued_at": None,
            "interval": new_interval,
        }


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_cancel_default_recalibration(client, redis_conn: Redis, worker):
    """POST /recalibration/cancel cancels the default recalibration started on startup"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)

        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/recalibration/cancel", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        assert queue.jobs == []
        assert queue.canceled_job_registry.count == 1
        assert queue.scheduled_job_registry.count == 0
        assert queue.finished_job_registry.count == 0
        assert queue.failed_job_registry.count == 0
        assert queue.deferred_job_registry.count == 0


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_init_recalibration_reads_a_naive_start_time_as_utc(
    client, redis_conn, worker, monkeypatch
):
    """POST /recalibration/init schedules a start time without a timezone in UTC,
    whatever the server's own timezone"""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        with client as client:
            queue_name = f"{get_queue_context()['queue_prefix']}_recalibration"
            registry = get_queue_by_name(worker, queue_name).scheduled_job_registry
            start_utc = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
                hours=1
            )

            client.post(
                "/recalibration/init",
                headers=create_mss_headers(is_admin=True),
                params={"start_timestamp": start_utc.replace(tzinfo=None).isoformat()},
            )

            [rq_job_id] = registry.get_job_ids()
            assert registry.get_scheduled_time(rq_job_id) == start_utc
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_failed_recalibration_schedules_the_next(client, redis_conn, worker, mocker):
    with client as client:
        queue_name = f"{get_queue_context()['queue_prefix']}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        failing_executor = Mock(recalibrate=Mock(side_effect=RuntimeError("no device")))
        mocker.patch(
            "app.services.scheduler.tasks.get_quantum_executor",
            return_value=failing_executor,
        )

        worker.work(burst=True, max_jobs=1)

        assert queue.failed_job_registry.count == 1
        assert queue.scheduled_job_registry.count == 1


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_cancel_recalibration_while_one_runs(client, redis_conn, worker, mocker):
    """POST /recalibration/cancel during a recalibration leaves none to follow it"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS[:1])[0]["id"]
        queue_name = f"{get_queue_context()['queue_prefix']}_recalibration"
        queue = get_queue_by_name(worker, queue_name)

        def cancel_recalibration():
            headers = create_mss_headers(user_id, is_admin=True)
            client.post("/recalibration/cancel", headers=headers)

        cancelling_executor = Mock(recalibrate=Mock(side_effect=cancel_recalibration))
        mocker.patch(
            "app.services.scheduler.tasks.get_quantum_executor",
            return_value=cancelling_executor,
        )

        worker.work(burst=True, max_jobs=1)

        assert queue.jobs == []
        assert queue.scheduled_job_registry.count == 0


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_cancel_restarted_recalibration(client, redis_conn: Redis, worker):
    """POST /recalibration/cancel cancels any recalibration restarted even after cancellation"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)

        # first cancel the recalibration
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/recalibration/cancel", headers=headers)
        assert response.status_code == 200

        # restart the recalibration with a given interval
        headers = create_mss_headers(user_id, is_admin=True)
        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)
        response = client.post(
            "/recalibration/init",
            headers=headers,
            params={
                "start_timestamp": start_timestamp.isoformat(),
                "interval": 4.0,
            },
        )
        assert response.status_code == 200

        assert queue.canceled_job_registry.count == 1
        assert queue.scheduled_job_registry.count == 1

        # Cancel the recalibration again
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/recalibration/cancel", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "success"}

        assert queue.jobs == []
        assert queue.canceled_job_registry.count == 2
        assert queue.scheduled_job_registry.count == 0
        assert queue.finished_job_registry.count == 0
        assert queue.failed_job_registry.count == 0
        assert queue.deferred_job_registry.count == 0


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_switch_off(client, redis_conn, jobs_folder, worker, job, mocker, fake_clock):
    """POST '/switch/off' switches off the scheduler and does not allow jobs"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]

        user = users[0]
        user_id = user["id"]

        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/switch/off", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        status = client.get("/switch/status", headers=create_mss_headers(user_id))
        assert status.json() == {"status": "OFF"}

        # submit many jobs from many users
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        name = os.environ.get("BCC_DEFAULT_PREFIX")
        assert response.status_code == 503
        assert response.json() == {"detail": f"{name} is offline"}

        worker.work(burst=True)

        jobs_in_redis = get_jobs_in_redis(redis_conn)
        assert jobs_in_redis == []


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_switch_on(client, redis_conn, jobs_folder, worker, job, mocker, fake_clock):
    """POST '/switch/on' switches on the scheduler and allowing jobs"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:2])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_metadata_list = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]

        user = users[0]
        user_id = user["id"]

        # first switch off
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/switch/off", headers=headers)
        assert response.status_code == 200

        # switch on again
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/switch/on", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        status = client.get("/switch/status", headers=create_mss_headers(user_id))
        assert status.json() == {"status": "ON"}

        # submit many jobs from many users
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        assert response.status_code == 200

        worker.work(burst=True)

        jobs_in_redis = get_jobs_in_redis(redis_conn)
        assert len(jobs_in_redis) == 1


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_switch_answers_502_when_mss_refuses_the_event(client, mocker):
    with client as client:
        mocker.patch.object(MssClient, "send")
        mocker.patch.object(
            MssClient,
            "recv",
            return_value={"status": "failed", "detail": "no such device"},
        )
        response = client.post("/switch/off", headers=create_mss_headers(is_admin=True))

        assert response.status_code == 502


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_switch_answers_504_when_mss_does_not_answer(client, mocker):
    with client as client:
        mocker.patch.object(MssClient, "send")
        mocker.patch.object(MssClient, "recv", side_effect=TimeoutError("no response"))
        response = client.post("/switch/off", headers=create_mss_headers(is_admin=True))

        assert response.status_code == 504


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_switch_answers_other_requests_while_it_tells_mss(client, mocker):
    """POST '/switch/off' waiting on MSS leaves the API free to answer other requests"""
    is_sending = threading.Event()
    is_other_request_answered = threading.Event()
    answered_while_sending = []

    def send_event_slowly(*args, **kwargs):
        is_sending.set()
        answered_while_sending.append(is_other_request_answered.wait(timeout=2))

    mss_client = mocker.patch("app.services.scheduler.service.get_mss_client")
    mss_client.return_value.send_event.side_effect = send_event_slowly
    with client as client:
        headers = create_mss_headers(is_admin=True)
        switch_off = threading.Thread(
            target=client.post, args=("/switch/off",), kwargs={"headers": headers}
        )
        switch_off.start()
        is_sending.wait(timeout=2)

        response = client.get("/switch/status", headers=create_mss_headers())
        is_other_request_answered.set()
        switch_off.join()

        assert response.status_code == 200
        assert answered_while_sending == [True]


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_switch_status(client, redis_conn, jobs_folder, worker, job, mocker):
    """GET '/switch/status' shows the status of the device if ON or OFF"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:2])
        user = users[0]
        user_id = user["id"]

        # check the status
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/switch/status", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "ON"}

        # first switch off
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/switch/off", headers=headers)
        assert response.status_code == 200

        # check status after switching off
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/switch/status", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "OFF"}

        # switch on again
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/switch/on", headers=headers)
        assert response.status_code == 200

        # check the status
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/switch/status", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "ON"}
