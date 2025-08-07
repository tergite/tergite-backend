# This code is part of Tergite
#
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
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from itertools import islice, zip_longest
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    TypedDict,
    TypeVar,
)
from uuid import uuid4

import pytest
from black.nodes import Generic
from pydantic import ValidationError
from pytest_mock import MockerFixture
from redis import Redis

from app.libs.queues.dtos import Job, JobStatus, Stage, Timestamps
from app.services.booking.models import Booking
from app.tests.conftest import (
    CLIENT_AND_RQ_WORKER_TUPLES,
    INVALID_CREATE_BOOKINGS_PARAMS,
    JOBS,
    JOBS_HASH_NAME,
    PAGINATION,
    USERS,
    VALID_BOOKINGS,
    VALID_CREATE_BOOKINGS_PARAMS,
    _BasicBookingInfo,
    _PaginationInfo,
)
from app.tests.utils.api import create_mss_headers
from app.tests.utils.env import TEST_MAX_SLOTS_PER_DAY
from app.tests.utils.fixtures import load_fixture
from app.tests.utils.records import order_by

if TYPE_CHECKING:
    from httpx import Response
    from starlette.testclient import TestClient

T = TypeVar("T")

_SIM_1Q_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload_simulator_1q.json")
_SIM_2Q_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload_simulator_2q.json")
_QUANTIFY_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload.json")
_INVALID_JOBS_FOR_UPLOAD = load_fixture("invalid_jobs_to_upload.json")

# params
_QUANTIFY_UPLOAD_JOB_PARAMS = [
    (*CLIENT_AND_RQ_WORKER_TUPLES[0], job) for job in _QUANTIFY_JOBS_FOR_UPLOAD
]

_SIM_1Q_UPLOAD_JOB_PARAMS = [
    (*CLIENT_AND_RQ_WORKER_TUPLES[1], job) for job in _SIM_1Q_JOBS_FOR_UPLOAD
]

_SIM_2Q_UPLOAD_JOB_PARAMS = [
    (*CLIENT_AND_RQ_WORKER_TUPLES[2], job) for job in _SIM_2Q_JOBS_FOR_UPLOAD
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


@pytest.mark.parametrize("user", USERS)
def test_create_user(quantify_rest_client, user):
    """POST to '/users' should create the user in the REST API

        curl -L 'http://127.0.0.1:5000/users' \
        -H 'Content-Type: application/json' \
        --data-raw '{
            "name": "John Doe",
            "email": "johndoe@example.com",
            "password": "some-password-for-john"
        }'
    """
    with quantify_rest_client as client:
        user_id, response = _create_user(client, user=user)
        expected = {
            "id": user_id,
            "name": user["name"],
            "email": user["email"],
            "is_admin": False,
        }

        assert response.status_code == 200
        assert response.json() == expected


@pytest.mark.parametrize("user", USERS)
def test_view_profile(quantify_rest_client, user, mocker: MockerFixture):
    """GET '/me' should show the user profile in the REST API

    curl -L 'http://127.0.0.1:5000/me' \
    -H 'Authorization: Bearer access-token-for-john'
    """
    with quantify_rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _get_token(client, mocker=mocker, data=user)

        response = _view_own_profile(client, token=token)
        profile = response.json()
        expected = {
            "id": user_id,
            "name": user["name"],
            "email": user["email"],
            "is_admin": False,
        }

        assert response.status_code == 200
        assert profile == expected


def test_create_root_user(quantify_rest_client):
    """POST to '/root-user' creates the root user only once per app instance

        curl -L 'http://127.0.0.1:5000/root-user' \
        -H 'Content-Type: application/json' \
        --data-raw '{
            "name": "Peninah Doe",
            "email": "peninahdoe@example.com",
            "password": "some-password-for-peninah"
        }'
    """
    with quantify_rest_client as client:
        user = USERS[0]
        other_user = USERS[1]
        user_id, response = _create_root_user(client, user=user)
        expected = {
            "id": user_id,
            "name": user["name"],
            "email": user["email"],
            "is_admin": True,
        }

        assert response.status_code == 200
        assert response.json() == expected

        # trying another user errs
        _, response = _create_root_user(client, user=other_user)
        expected = {"detail": "root user already exists"}
        assert response.status_code == 405
        assert response.json() == expected


@pytest.mark.parametrize("pagination", PAGINATION)
def test_admin_view_users(
    quantify_rest_client, pagination: "_PaginationInfo", mocker: MockerFixture
):
    """GET '/users' should show to an admin the paginated list of user profiles

    curl -L 'http://127.0.0.1:5000/users?skip=1&limit=10' \
    -H 'Authorization: Bearer access-token-for-john'
    """
    with quantify_rest_client as client:
        # create admin user and token
        admin_user_data, *other_user_data = USERS
        admin_user_id, _ = _create_root_user(client, user=admin_user_data)
        admin_token, _ = _get_token(client, mocker=mocker, data=admin_user_data)

        # create many users and their tokens
        user_id_token_map = _create_user_token_map(
            client, mocker=mocker, raw_users=other_user_data
        )
        non_admin_user_id = next(islice(user_id_token_map, 0, 1))
        non_admin_token = user_id_token_map[non_admin_user_id]

        skip = pagination["skip"]
        limit = pagination["limit"]

        # non admins are not allowed
        response = _view_user_list(
            client, token=non_admin_token, skip=skip, limit=limit
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "unauthorized"}

        # admins are allowed
        response = _view_user_list(client, token=admin_token, skip=skip, limit=limit)
        actual_output = response.json()

        profiles = [
            {
                "id": admin_user_id,
                "name": admin_user_data["name"],
                "email": admin_user_data["email"],
                "is_admin": True,
            },
            *[
                {
                    "id": id_,
                    "is_admin": False,
                    "name": data["name"],
                    "email": data["email"],
                }
                for id_, data in zip(user_id_token_map.keys(), USERS[1:])
            ],
        ]
        expected = _paginate(profiles, skip=skip, limit=limit)

        assert response.status_code == 200
        assert actual_output == expected


@pytest.mark.timeout(180)
def test_delete_profile(
    quantify_rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """DELETE '/me' should delete their own user profile

    curl -L -X DELETE 'http://127.0.0.1:5000/me' \
    -H 'Authorization: Bearer access-token-for-john'
    """
    with quantify_rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)
        user_id = next(islice(user_id_token_map, 0, 1))
        token = user_id_token_map[user_id]

        other_user_id = next(islice(user_id_token_map, 1, 2))
        other_token = user_id_token_map[other_user_id]

        # create many bookings for this user
        for booking in VALID_BOOKINGS[:TEST_MAX_SLOTS_PER_DAY]:
            _create_booking(client, token=token, booking=booking)

        # create a booking for other user
        _create_booking(
            client, token=other_token, booking={"starts_in": 8, "duration": 2}
        )

        # wait for the first booking to start
        _submit_multiple_jobs(
            client, user_id_token_map=user_id_token_map, is_job_id_specified=False
        )

        # This should also cancel any bookings and any jobs belonging to user
        response = _delete_own_profile(client, token=token)
        assert response.status_code == 200
        assert response.json() == {"status": "success", "detail": "Profile deleted"}

        response = _view_own_profile(client, token=token)
        assert response.status_code == 404
        assert response.json() == {"detail": "user not found"}

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = rq_worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            rq_worker.work(burst=True, with_scheduler=True)

        jobs_in_redis = _get_jobs_in_redis(redis_client)

        deleted_user_jobs = [job for job in jobs_in_redis if job.user_id == user_id]
        other_user_jobs = [job for job in jobs_in_redis if job.user_id != user_id]
        failure_reason = "Cancelled on user deletion"

        assert all([v.status == JobStatus.CANCELLED for v in deleted_user_jobs])
        assert all([v.failure_reason == failure_reason for v in deleted_user_jobs])
        assert all([v.status == JobStatus.SUCCESSFUL for v in other_user_jobs])


@pytest.mark.timeout(180)
def test_admin_remove_user(
    quantify_rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """DELETE '/users/{user_id}' by ddmin removes the user, and their bookings and jobs

    curl -L -X DELETE 'http://127.0.0.1:5000/users/janes-user-id' \
    -H 'Authorization: Bearer access-token-for-john'
    """
    with quantify_rest_client as client:
        # create admin user and token
        admin_user_data, *other_user_data = USERS
        admin_user_id, _ = _create_root_user(client, user=admin_user_data)
        admin_token, _ = _get_token(client, mocker=mocker, data=admin_user_data)

        # create many users and their tokens
        user_id_token_map = _create_user_token_map(
            client, mocker=mocker, raw_users=other_user_data
        )
        curr_user_id = next(islice(user_id_token_map, 0, 1))
        curr_token = user_id_token_map[curr_user_id]

        # create many bookings for this user
        for booking in VALID_BOOKINGS[:TEST_MAX_SLOTS_PER_DAY]:
            _, result = _create_booking(client, token=curr_token, booking=booking)

        # create a booking for admin user
        _create_booking(
            client, token=admin_token, booking={"starts_in": 8, "duration": 2}
        )

        # wait for the first booking to start
        _submit_multiple_jobs(
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=False,
        )

        # This also removes the user's pending bookings,
        # and cancels their pending jobs
        response = _remove_user(client, token=admin_token, user_id=curr_user_id)
        assert response.status_code == 200
        assert response.json() == {"status": "success", "detail": f"User deleted"}

        response = _view_own_profile(client, token=curr_token)
        assert response.status_code == 404
        assert response.json() == {"detail": "user not found"}

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = rq_worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            rq_worker.work(burst=True, with_scheduler=True)

        jobs_in_redis = _get_jobs_in_redis(redis_client)

        deleted_user_jobs = [
            job for job in jobs_in_redis if job.user_id == curr_user_id
        ]
        other_user_jobs = [job for job in jobs_in_redis if job.user_id != curr_user_id]
        failure_reason = "Cancelled on user deletion"

        assert all([v.status == JobStatus.CANCELLED for v in deleted_user_jobs])
        assert all([v.failure_reason == failure_reason for v in deleted_user_jobs])
        assert all([v.status == JobStatus.SUCCESSFUL for v in other_user_jobs])


@pytest.mark.timeout(180)
def test_non_admin_remove_user(
    quantify_rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """Non admin cannot remove the profile of any user"""
    with quantify_rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)
        curr_user_id = next(islice(user_id_token_map, 0, 1))
        curr_token = user_id_token_map[curr_user_id]

        other_user_id = next(islice(user_id_token_map, 1, 2))
        other_token = user_id_token_map[other_user_id]

        # create many bookings for this user
        for booking in VALID_BOOKINGS[:TEST_MAX_SLOTS_PER_DAY]:
            _, result = _create_booking(client, token=curr_token, booking=booking)

        # wait for the first booking to start
        _submit_multiple_jobs(
            client, user_id_token_map=user_id_token_map, is_job_id_specified=False
        )

        # Non-admins fail
        for token in (curr_token, other_token):
            response = _remove_user(client, token=token, user_id=curr_user_id)
            assert response.status_code == 403
            assert response.json() == {"detail": "unauthorized"}

        response = _view_own_profile(client, token=curr_token)
        assert response.status_code == 200

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = rq_worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            rq_worker.work(burst=True, with_scheduler=True)

        jobs_in_redis = _get_jobs_in_redis(redis_client)
        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])


@pytest.mark.parametrize("user", USERS)
def test_login(quantify_rest_client, user, mocker: MockerFixture):
    """POST '/login' should return the JWT token for the user

    curl -L 'http://127.0.0.1:5000/login' \
    -H 'Content-Type: application/json' \
    --data-raw '{
        "email": "johndoe@example.com",
        "password": "some-password-for-john"
    }'
    """
    with quantify_rest_client as client:
        _create_user(client, user=user)
        token, response = _get_token(client, mocker=mocker, data=user)

        assert response.status_code == 200
        assert response.json() == {"access_token": token, "token_type": "bearer"}


@pytest.mark.parametrize("user,booking", VALID_CREATE_BOOKINGS_PARAMS)
def test_create_booking(
    quantify_rest_client, user, booking: "_BasicBookingInfo", mocker: MockerFixture
):
    """POST '/bookings' should return the new booking created by the user

    curl -L 'http://127.0.0.1:5000/bookings' \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer access-token-for-john' \
    -d '{
        "start_utc": "2025-07-18T17:48:58.619Z",
        "end_utc": "2025-07-18T17:52:58.619Z"
    }'
    """
    with quantify_rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _get_token(client, mocker=mocker, data=user)

        # create booking
        actual_booking_info, response = _create_booking(
            client, token=token, booking=booking
        )
        actual_booking = response.json()

        assert response.status_code == 200

        assert actual_booking["id"] != ""
        assert actual_booking_info["duration"] == booking["duration"]
        assert actual_booking_info["starts_in"] == booking["starts_in"]
        assert actual_booking["user_id"] == user_id


@pytest.mark.parametrize("user,booking", INVALID_CREATE_BOOKINGS_PARAMS)
def test_create_invalid_booking(
    quantify_rest_client, user, booking: "_BasicBookingInfo", mocker: MockerFixture
):
    """Should return an error message if an attempt to create an invalid booking is made

    e.g. start_utc in the past
    """
    with quantify_rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _get_token(client, mocker=mocker, data=user)
        data = _to_booking_payload(booking)

        # create booking
        headers = _get_headers(token)
        response = client.post("/bookings", headers=headers, json=data)
        detail = f"{response.json()["detail"]}"

        assert response.status_code == 422
        assert booking["error_message"] in detail


@pytest.mark.parametrize("user,booking", VALID_CREATE_BOOKINGS_PARAMS)
def test_create_conflicting_booking(
    quantify_rest_client, user, booking: "_BasicBookingInfo", mocker: MockerFixture
):
    """Should return an error message when creating a booking overlapping with another"""
    with quantify_rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _get_token(client, mocker=mocker, data=user)

        # create booking
        _, response = _create_booking(client, token=token, booking=booking)
        initial_slot = response.json()

        # create another
        overlapping_slot = {**booking, "duration": booking["duration"] + 10}
        headers = _get_headers(token)
        data = _to_booking_payload(overlapping_slot)
        response = client.post("/bookings", headers=headers, json=data)
        json_response = response.json()

        start_utc = datetime.fromisoformat(initial_slot["start_utc"]).replace(
            tzinfo=None
        )
        end_utc = datetime.fromisoformat(initial_slot["end_utc"]).replace(tzinfo=None)
        expected_err = (
            f"booking conflicts with another booking at {start_utc} to {end_utc}"
        )
        assert response.status_code == 400
        assert expected_err in json_response["detail"]


@pytest.mark.parametrize("user", USERS)
def test_create_too_many_booking(quantify_rest_client, user, mocker: MockerFixture):
    """Returns error message when creating a booking when number of bookings in a period for user are maxed out."""
    with quantify_rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _get_token(client, mocker=mocker, data=user)

        max_safe_idx = TEST_MAX_SLOTS_PER_DAY - 1

        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        expected_err = f"you have exceeded the maximum {TEST_MAX_SLOTS_PER_DAY} bookings per day for {day_start}-{day_end}"

        # create many bookings
        for idx, raw_item in enumerate(VALID_BOOKINGS):
            headers = _get_headers(token)
            data = _to_booking_payload(raw_item)
            response = client.post("/bookings", headers=headers, json=data)
            json_response = response.json()

            if idx > max_safe_idx:
                assert expected_err in json_response["detail"]
            else:
                assert response.status_code == 200


def test_create_booking_invalid_token(quantify_rest_client, rq_worker, redis_client):
    """Creating booking with invalid tokens results in an error"""
    with quantify_rest_client as client:
        headers = _get_headers("token")
        data = _to_booking_payload(VALID_BOOKINGS[0])

        response = client.post("/bookings", headers=headers, json=data)
        json_response = response.json()

        assert response.status_code == 401
        assert "not authenticated" in json_response["detail"]


def test_create_booking_without_token(quantify_rest_client, rq_worker, redis_client):
    """Creating booking without a token results in an error"""
    with quantify_rest_client as client:
        data = _to_booking_payload(VALID_BOOKINGS[0])

        response = client.post("/bookings", data=data)
        json_response = response.json()

        assert response.status_code == 401
        assert json_response["detail"] == "Not authenticated"


@pytest.mark.parametrize("pagination", PAGINATION)
def test_view_bookings(
    quantify_rest_client, mocker: MockerFixture, pagination: "_PaginationInfo"
):
    """GET "/bookings" shows paginated list of all available bookings"""
    with quantify_rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)
        curr_user_id = next(islice(user_id_token_map, 0, 1))
        curr_token = user_id_token_map[curr_user_id]

        other_user_id = next(islice(user_id_token_map, 1, 2))
        other_token = user_id_token_map[other_user_id]

        # create bookings, for each user only upto TEST_MAX_SLOTS_PER_DAY
        tokens = [curr_token, other_token]
        all_records = []
        booking_data_list = VALID_BOOKINGS[: 2 * TEST_MAX_SLOTS_PER_DAY]
        for idx, booking_info in enumerate(booking_data_list):
            tokens_idx = idx % 2
            token = tokens[tokens_idx]
            _, response = _create_booking(client, token=token, booking=booking_info)
            all_records.append(response.json())

        limit = pagination["limit"]
        skip = pagination["skip"]

        # view bookings
        for token in tokens:
            response = _view_booking_list(client, token=token, skip=skip, limit=limit)
            actual_output = response.json()
            expected = _paginate(all_records, skip=skip, limit=limit)

            assert response.status_code == 200
            assert actual_output == expected


def test_view_bookings_invalid_token(quantify_rest_client, rq_worker, redis_client):
    """Viewing bookings with invalid tokens results in an error"""
    with quantify_rest_client as client:
        response = _view_booking_list(client, token="token")
        json_response = response.json()

        assert response.status_code == 401
        assert "not authenticated" in json_response["detail"]


def test_view_bookings_without_token(quantify_rest_client, rq_worker, redis_client):
    """Viewing bookings without a token results in an error"""
    with quantify_rest_client as client:
        response = client.get("/bookings")
        json_response = response.json()

        assert response.status_code == 401
        assert json_response["detail"] == "Not authenticated"


@pytest.mark.timeout(240)
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
):
    """POST '/jobs' when there is no booking should run the jobs in FIFO (first in, first out)"""
    with client as client:
        # create many users and their tokens
        users = _create_many_users(client)

        durations = [0.4, 3, 4, 1.1, 2.9, 0.3, 0.1]
        raw_jobs = _get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        job_ids_in_fifo = _submit_multiple_jobs_v2(client, data=job_metadata_list)

        # Run the queue
        worker.work(burst=True)

        jobs_in_redis = _get_jobs_in_redis(redis_conn)
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
            assert _job_results_match(
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


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_in_active_booking(
    client,
    worker,
    redis_conn,
    job,
    jobs_folder,
    mocker: MockerFixture,
):
    """POST '/jobs' when there is an active booking runs the booker jobs first then runs the other jobs after booking."""

    with client as client:
        # create many users and their tokens
        users = _create_many_users(client)

        durations = [0.23, 2.0, 2.1, 10]
        raw_jobs = _get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # create booking
        # third user; thus third job (duration: 2.1) belongs to them
        booker = users[2]
        booker_id = booker["id"]
        # duration: 3; max idle time = 1
        booking_info = {"duration": 3, "starts_in": 0}
        _, response = _create_booking_v2(
            client, user_id=booker_id, booking=booking_info
        )
        booking = Booking.model_validate(response.json())

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            worker.work(burst=True, with_scheduler=True)

        jobs_in_redis = _get_jobs_in_redis(redis_conn)
        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)

        booker_jobs = [job for job in jobs_in_redis if job.user_id == booker_id]
        non_booker_jobs = [job for job in jobs_in_redis if job.user_id != booker_id]
        last_booker_job = booker_jobs[-1]
        first_non_booker_job = non_booker_jobs[0]

        # Assert that they are all complete and their booker jobs started before booking end_utc
        # while non booker jobs started after end_utc
        # Note: Enqueue_at ignores microseconds
        booking_end_timestamp = _drop_microsec(booking.end_utc)
        last_booker_job_start = _drop_microsec(
            last_booker_job.timestamps.execution.start_timestamp
        )
        first_non_booker_job_start = _drop_microsec(
            first_non_booker_job.timestamps.execution.start_timestamp
        )

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert last_booker_job_start < booking_end_timestamp
        assert first_non_booker_job_start >= booking_end_timestamp


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_in_idle_booking(
    client,
    worker,
    redis_conn,
    job,
    jobs_folder,
    mocker,
):
    """POST '/jobs' when there is an idle booking runs the non booker jobs even during the booking."""
    with client as client:
        # create many users and their tokens
        users = _create_many_users(client)

        durations = [0.23, 2.0, 2.1, 10]
        raw_jobs = _get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker = users[0]
        booker_id = booker["id"]
        booking_info = {"duration": 5, "starts_in": 0}
        _, response = _create_booking_v2(
            client, user_id=booker_id, booking=booking_info
        )
        booking = Booking.model_validate(response.json())

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            worker.work(burst=True, with_scheduler=True)

        jobs_in_redis = _get_jobs_in_redis(redis_conn)

        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        booker_jobs = [job for job in jobs_in_redis if job.user_id == booker_id]
        non_booker_jobs = [job for job in jobs_in_redis if job.user_id != booker_id]
        last_booker_job = booker_jobs[-1]
        first_non_booker_job = non_booker_jobs[0]

        # Assert that they are all complete and their booker jobs started before booking end_utc
        # while non booker jobs started after end_utc
        # Note: Enqueue_at ignores microseconds
        booking_end_timestamp = _drop_microsec(booking.end_utc)
        last_booker_job_start = _drop_microsec(last_booker_job.start_utc)
        first_non_booker_job_start = _drop_microsec(first_non_booker_job.start_utc)

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert last_booker_job_start < booking_end_timestamp
        if last_booker_job.actual_duration < booking.total_duration:
            assert first_non_booker_job_start < booking_end_timestamp


# _SIMPLE_UPLOAD_JOB_PARAMS[:-1] because the 2-qubit execution, processing takes long
@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    "client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS[:-1]
)
def test_submit_jobs_in_idle_booking_before_another(
    client,
    worker,
    redis_conn,
    job,
    jobs_folder,
    mocker,
):
    """When there is an idle booking and another is next, only non booker jobs, short enough to fit in between, run."""
    with client as client:
        # create many users and their tokens
        users = _create_many_users(client)

        durations = [0.2, 3, 4, 1, 2.9, 0.3, 0.15]
        raw_jobs = _get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker = users[0]
        booker_id = booker["id"]

        # create first booking
        first_booking_info = {"duration": 2, "starts_in": 0}
        _, result = _create_booking_v2(
            client, user_id=booker_id, booking=first_booking_info
        )

        # create second booking
        second_booking_info = {"duration": 3, "starts_in": 3}
        _, response = _create_booking_v2(
            client, user_id=booker_id, booking=second_booking_info
        )
        next_slot = Booking.model_validate(response.json())

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            worker.work(burst=True, with_scheduler=True)

        jobs_in_db = _get_jobs_in_redis(redis_conn)

        booker_jobs = [v for v in jobs_in_db if v.user_id == booker_id]
        non_booker_jobs = [v for v in jobs_in_db if v.user_id != booker_id]

        booker_jobs.sort(key=lambda v: v.estimated_duration)
        non_booker_jobs.sort(key=lambda v: v.start_utc)
        # Note: Enqueue_at ignores microseconds
        non_booker_job_ids_pre_2nd_slot = [
            v.job_id
            for v in non_booker_jobs
            if _drop_microsec(v.start_utc) < _drop_microsec(next_slot.start_utc)
        ]
        non_booker_job_ids_post_2nd_slot = [
            v.job_id
            for v in non_booker_jobs
            if _drop_microsec(v.start_utc) > _drop_microsec(next_slot.start_utc)
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
        # the fourth is too long for the current booking so it fails immediately.
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
        # non booker jobs short enough to finish before next booking (after waiting for 1 second timeout on booked queue)
        assert (
            non_booker_job_ids_pre_2nd_slot == expected_non_booker_job_ids_pre_2nd_slot
        )
        # non booker jobs too long to finish before next booking run after the new booking.
        assert (
            non_booker_job_ids_post_2nd_slot
            == expected_non_booker_job_ids_post_2nd_slot
        )


# _SIMPLE_UPLOAD_JOB_PARAMS[:-1] because the 2-qubit execution, processing takes long
@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    "client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS[:-1]
)
def test_submit_long_jobs_before_booking(
    client,
    worker,
    redis_conn,
    job,
    jobs_folder,
    mocker,
):
    """POST long jobs to '/jobs' before booking starts waitlists them but runs the shorter ones"""
    with client as client:
        # create many users and their tokens
        users = _create_many_users(client)

        durations = [0.3, 3, 4, 1, 2.9, 0.2, 0.1]
        raw_jobs = _get_raw_jobs(job, durations)
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker = users[0]
        booker_id = booker["id"]

        # create booking
        booking_info = {"duration": 3, "starts_in": 2.2}
        _create_booking_v2(client, user_id=booker_id, booking=booking_info)

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            worker.work(burst=True, with_scheduler=True)

        jobs_in_db = _get_jobs_in_redis(redis_conn)
        jobs_in_db.sort(key=lambda v: v.start_utc)
        job_estimated_durations = [v.estimated_duration for v in jobs_in_db]

        assert all([job.status == JobStatus.SUCCESSFUL for job in jobs_in_db])
        # the small-enough first (0.3, 1, 0.2, 0.1),
        # then the bigger ones (3.0, 4.0, 2.9) in original order
        assert job_estimated_durations == [0.3, 1, 0.2, 0.1, 3.0, 4.0, 2.9]


@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_invalid_token(client, redis_conn, worker, job, jobs_folder):
    """POST '/jobs' with invalid tokens results in an error"""
    with client as client:
        headers = _get_headers("token")
        job_file_path = _save_job_file(folder=jobs_folder, job=job)
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
        job_file_path = _save_job_file(folder=jobs_folder, job=job)
        with open(job_file_path, "rb") as file:
            response = client.post("/jobs", files={"upload_file": file})

        json_response = response.json()

        assert response.status_code == 401
        assert json_response["detail"] == "Unauthorized"


@pytest.mark.parametrize(
    "client, redis_conn, worker, job", _ALL_INVALID_UPLOAD_JOB_PARAMS
)
def test_upload_invalid_job(client, redis_conn, worker, job, jobs_folder, mocker):
    """POSTing an invalid structure of a job to '/jobs' fails"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = _create_many_users(client, raw_users=USERS[:1])
        raw_jobs = _get_raw_jobs(job, durations=[0.2])
        job_metadata = _get_job_submission_metadata(
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
        jobs_in_db = _get_jobs_in_redis(redis_conn)
        assert jobs_in_db == []


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_duplicate_job_upload(client, redis_conn, jobs_folder, worker, job, mocker):
    """Uploading two jobs of the same job_id returns an error"""
    with client as client:
        users = _create_many_users(client, raw_users=USERS[:1])
        raw_jobs = _get_raw_jobs(job, durations=[0.2])
        job_metadata = _get_job_submission_metadata(
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
            token, _ = _get_token(client, mocker, data=login_details)
            fresh_headers = _get_headers(token)
            third_response = client.post(
                "/jobs", files={"upload_file": file}, headers=fresh_headers
            )

            worker.work(burst=True)

    expected_err_resp = {"detail": f"job {job_info["job_id"]} already exists"}
    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json() == expected_err_resp
    assert third_response.status_code == 409
    assert third_response.json() == expected_err_resp


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_remove_job(client, redis_conn, jobs_folder, worker, job, mocker):
    """DELETE to '/jobs/{job_id}' deletes the given job if job belongs to user"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = _create_many_users(client, raw_users=USERS[:2])
        raw_jobs = _get_raw_jobs(job, durations=[0.2, 1, 0.3])
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

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

        jobs_in_db = _get_jobs_in_redis(redis_conn)
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list[1:]])
        assert deletion_response.status_code == 200
        assert job_ids_in_db == expected_ids


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_remove_another_users_job(client, redis_conn, jobs_folder, worker, job, mocker):
    """DELETE to '/jobs/{job_id}' fails if the user does not own the given job"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = _create_many_users(client, raw_users=USERS[:2])
        raw_jobs = _get_raw_jobs(job, durations=[0.2, 1, 0.3])
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

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

        jobs_in_db = _get_jobs_in_redis(redis_conn)
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list])
        assert deletion_response.status_code == 404
        assert deletion_response.json() == {"detail": f"Job {job_id} not found"}
        assert job_ids_in_db == expected_ids


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_admin_remove_job(client, redis_conn, jobs_folder, worker, job, mocker):
    """DELETE to '/jobs/{job_id}' deletes the given job if user is admin"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = _create_many_users(client, raw_users=USERS[:2])
        raw_jobs = _get_raw_jobs(job, durations=[0.2, 1, 0.3])
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

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

        jobs_in_db = _get_jobs_in_redis(redis_conn)
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list[1:]])
        assert deletion_response.status_code == 200
        assert job_ids_in_db == expected_ids


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_remove_job(
    client, redis_conn, jobs_folder, worker, job, mocker
):
    """Delete to /jobs/{job_id} returns 401 error when accessed outside MSS or without proper MSS headers"""
    # using context manager to ensure on_startup runs
    with client as client:
        users = _create_many_users(client, raw_users=USERS[:2])
        raw_jobs = _get_raw_jobs(job, durations=[0.2, 1, 0.3])
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

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

        jobs_in_db = _get_jobs_in_redis(redis_conn)
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list])
        assert job_ids_in_db == expected_ids


@pytest.mark.timeout(180)
def test_cancel_future_booking(
    quantify_rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/booking/{id}/cancel' for a future booking, deletes the booking and allows jobs to run without it."""
    with quantify_rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)

        # create booking
        # third user; thus third job (duration: 2.1) belongs to them
        booker_user_id = next(islice(user_id_token_map, 2, 3))
        booker_token = user_id_token_map[booker_user_id]
        booking_info = {"duration": 3, "starts_in": 2}
        _, response = _create_booking(client, token=booker_token, booking=booking_info)
        booking = Booking.model_validate(response.json())

        response = _cancel_booking(client, token=booker_token, booking_id=booking.id)
        expected = {
            "status": "success",
            "detail": f"Booking of id {booking.id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected

        time.sleep(booking_info["starts_in"])
        # Booking does not exist or work
        # submit many jobs from many users when booking starts
        expected_job_ids = _submit_multiple_jobs(
            client, user_id_token_map=user_id_token_map, is_job_id_specified=False
        )

        # Run the queue
        rq_worker.work(burst=True)

        jobs_in_redis = _get_jobs_in_redis(redis_client)

        jobs_in_redis.sort(key=lambda v: v.start_utc)
        job_ids = [job.job_id for job in jobs_in_redis]

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert job_ids == expected_job_ids


def test_admin_cancel_future_booking(quantify_rest_client, mocker: MockerFixture):
    """Admin POST '/bookings/{id}/cancel' cancels any user's future booking"""
    with quantify_rest_client as client:
        # create admin user and token
        admin_user_data, *other_user_data = USERS
        admin_user_id, _ = _create_root_user(client, user=admin_user_data)
        admin_token, _ = _get_token(client, mocker=mocker, data=admin_user_data)

        # create many users and their tokens
        user_id_token_map = _create_user_token_map(
            client, mocker=mocker, raw_users=other_user_data
        )
        curr_user_id = next(islice(user_id_token_map, 0, 1))
        curr_token = user_id_token_map[curr_user_id]

        other_user_id = next(islice(user_id_token_map, 1, 2))
        other_token = user_id_token_map[other_user_id]

        # create booking
        booking_info = {"duration": 3, "starts_in": 2}
        _, response = _create_booking(client, token=curr_token, booking=booking_info)
        booking = Booking.model_validate(response.json())

        # Non-admins fail
        response = _cancel_booking(client, token=other_token, booking_id=booking.id)
        json_response = response.json()
        assert response.status_code == 404
        assert "not found" in json_response["detail"]

        response = _cancel_booking(client, token=admin_token, booking_id=booking.id)
        expected = {
            "status": "success",
            "detail": f"Booking of id {booking.id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected


@pytest.mark.timeout(180)
def test_cancel_active_booking(
    quantify_rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/bookings/{id}/cancel' for an active booking fails."""
    with quantify_rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)

        # create booking
        # third user; thus third job (duration: 2.1) belongs to them
        booker_user_id = next(islice(user_id_token_map, 2, 3))
        booker_token = user_id_token_map[booker_user_id]
        # duration: 3; max idle time = 1
        booking_info = {"duration": 3, "starts_in": 0}
        _, response = _create_booking(client, token=booker_token, booking=booking_info)
        booking = Booking.model_validate(response.json())

        response = _cancel_booking(client, token=booker_token, booking_id=booking.id)
        expected = {"detail": f"the booking of id {booking.id} is already active"}
        got = response.json()

        assert response.status_code == 400
        assert got == expected

        # It still works
        # submit many jobs from many users when booking starts
        _submit_multiple_jobs(
            client, user_id_token_map=user_id_token_map, is_job_id_specified=False
        )

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = rq_worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            rq_worker.work(burst=True, with_scheduler=True)

        jobs_in_redis = _get_jobs_in_redis(redis_client)

        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        booker_jobs = [job for job in jobs_in_redis if job.user_id == booker_user_id]
        last_booker_job = booker_jobs[-1]

        # Assert that they are all complete and their booker jobs started before booking end_utc
        # while non booker jobs started after end_utc
        # Note: Enqueue_at ignores microseconds
        booking_end_timestamp = _drop_microsec(booking.end_utc)
        last_booker_job_start = _drop_microsec(
            last_booker_job.timestamps.execution.start_timestamp
        )

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert last_booker_job_start < booking_end_timestamp


@pytest.mark.timeout(180)
def test_cancel_completed_booking(
    quantify_rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/bookings/{id}/cancel' for a completed booking fails."""
    with quantify_rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)

        # create booking
        booker_user_id = next(islice(user_id_token_map, 0, 1))
        booker_token = user_id_token_map[booker_user_id]
        # duration: 2; max idle time = 1
        booking_info = {"duration": 2, "starts_in": 0}
        _, response = _create_booking(client, token=booker_token, booking=booking_info)
        booking = Booking.model_validate(response.json())

        time.sleep(2)

        response = _cancel_booking(client, token=booker_token, booking_id=booking.id)
        expected = {"detail": f"the booking of id {booking.id} is already complete"}
        got = response.json()

        assert response.status_code == 400
        assert got == expected


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, worker, job, device_name, user", _VIEW_JOB_PARAMS)
def test_view_job(
    client, worker, job, device_name, user, jobs_folder, mocker: MockerFixture
):
    """GET '/jobs/{job_id}' by a user can show the job for the job_id if job belongs to them"""
    job_file_path = _save_job_file(folder=jobs_folder, job=job)

    with client as client:
        user_id, _ = _create_user(client, user=user)
        other_user_id, _ = _create_user(
            client, user={**user, "email": f"extra-{user["email"]}"}
        )
        job_id = job["job_id"]
        token, _ = _get_token(
            client, mocker, data={"user_id": user_id, "job_id": job_id}
        )

        # submit job
        headers = _get_headers(token)

        with open(job_file_path, "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=headers
            )

        expected_job = Job.model_validate(response.json())

        pending_job, _ = _view_job(client, user_id=user_id, job_id=job_id)
        assert pending_job == expected_job

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True)

        complete_job, _ = _view_job(client, user_id=user_id, job_id=job_id)
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


@pytest.mark.parametrize("client, worker, job, device_name, user", _VIEW_JOB_PARAMS)
def test_unauthenticated_view_job(
    client, worker, job, device_name, user, jobs_folder, mocker: MockerFixture
):
    """Get to /jobs/{job_id} raise 401 if not accessed through MSS"""
    job_file_path = _save_job_file(folder=jobs_folder, job=job)

    with client as client:
        user_id, _ = _create_user(client, user=user)
        job_id = job["job_id"]
        token, _ = _get_token(
            client, mocker, data={"user_id": user_id, "job_id": job_id}
        )

        # submit job
        headers = _get_headers(token)

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
    job_file_path = _save_job_file(folder=jobs_folder, job=job)

    with client as client:
        user_id, _ = _create_user(client, user=user)
        other_user_id, _ = _create_user(
            client, user={**user, "email": f"extra-{user["email"]}"}
        )
        job_id = job["job_id"]
        token, _ = _get_token(
            client, mocker, data={"user_id": user_id, "job_id": job_id}
        )

        # submit job
        headers = _get_headers(token)

        with open(job_file_path, "rb") as file:
            client.post("/jobs", files={"upload_file": file}, headers=headers)

        received_job, response = _view_job(client, user_id=other_user_id, job_id=job_id)
        assert received_job is None

        assert response.status_code == 404
        assert response.json() == {"detail": f"Job {job_id} not found"}


@pytest.mark.parametrize("client, worker, job, device_name, user", _VIEW_JOB_PARAMS[:3])
def test_admin_view_job(
    client, worker, job, device_name, user, jobs_folder, mocker: MockerFixture
):
    """Get to /jobs/{job_id} returns the job if user is admin"""
    job_file_path = _save_job_file(folder=jobs_folder, job=job)

    with client as client:
        user_id, _ = _create_user(client, user=user)
        other_user_id, _ = _create_user(
            client, user={**user, "email": f"extra-{user["email"]}"}
        )
        job_id = job["job_id"]
        token, _ = _get_token(
            client, mocker, data={"user_id": user_id, "job_id": job_id}
        )

        # submit job
        headers = _get_headers(token)

        with open(job_file_path, "rb") as file:
            resp = client.post("/jobs", files={"upload_file": file}, headers=headers)

        expected_job = Job.model_validate(resp.json())

        received_job, response = _view_job(
            client, user_id=other_user_id, job_id=job_id, is_admin=True
        )

        assert response.status_code == 200
        assert received_job == expected_job


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, _redis, worker, job, device", _VIEW_JOBS_PARAMS)
def test_view_jobs(
    client, _redis, worker, job, device, jobs_folder, mocker: MockerFixture
):
    """GET '/jobs' by a user can show the paginated list of jobs that belong to them"""
    with client as client:
        # create many users and their tokens
        users = _create_many_users(client)
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
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        job_ids = _submit_multiple_jobs_v2(client, data=job_metadata_list)

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

        assert pending_jobs_resp == _paginate(expected_jobs)

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
        assert completed_jobs_resp == _paginate(expected_jobs)


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, _redis, worker, job, device", _VIEW_JOBS_PARAMS)
def test_view_jobs_by_status(
    client, _redis, worker, job, device, jobs_folder, mocker: MockerFixture
):
    """GET '/jobs' by a user can show the paginated list of jobs that belong to them"""
    with client as client:
        # create many users and their tokens
        users = _create_many_users(client)
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
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        completed_job_ids = _submit_multiple_jobs_v2(client, data=job_metadata_list)

        # Run the queue to run the job and see that it is updated
        worker.work(burst=True)

        new_raw_jobs = [{**v, "job_id": f"{uuid4()}"} for v in raw_jobs]
        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users when booking starts
        pending_job_ids = _submit_multiple_jobs_v2(client, data=job_metadata_list)

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

        assert pending_jobs_resp == _paginate(expected_pending_jobs)

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
        assert completed_jobs_resp == _paginate(expected_completed_jobs)

        ## Get all jobs

        # refresh the headers
        headers = create_mss_headers(user_id)
        response = client.get("/jobs", headers=headers)
        all_jobs_resp = response.json()

        expected_all_jobs = expected_completed_jobs + expected_pending_jobs
        expected_all_jobs = order_by(expected_all_jobs, field="job_id")

        # ensure ordering is the same
        all_jobs_resp["data"] = order_by(all_jobs_resp["data"], field="job_id")
        assert all_jobs_resp == _paginate(expected_all_jobs)


@pytest.mark.parametrize("client, _redis, worker, job, device", _VIEW_JOBS_PARAMS)
def test_unauthenticated_view_jobs(
    client, _redis, worker, job, device, jobs_folder, mocker: MockerFixture
):
    """Get to /jobs/ raise 401 if not accessed through MSS"""
    with client as client:
        # create many users and their tokens
        users = _create_many_users(client)

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
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        job_ids = _submit_multiple_jobs_v2(client, data=job_metadata_list)

        response = client.get("/jobs")
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        # use a token used only for submitting jobs directly to the backend
        token, _ = _get_token(
            client, mocker, data={"user_id": user_id, "job_id": job_ids[0]}
        )
        headers = _get_headers(token)
        response = client.get(f"/jobs", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_job(client, redis_conn, jobs_folder, worker, job, mocker):
    """A user POST to '/jobs/{id}/cancel' cancels the job of the job_id if the job belongs to them"""
    with client as client:
        cancellation_reason = "just testing"

        users = _create_many_users(client, raw_users=USERS[:1])
        raw_jobs = _get_raw_jobs(job, durations=[0.2])
        job_metadata_list = _get_job_submission_metadata(
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

        response = _cancel_job(
            client, user_id=user_id, job_id=job_id, reason=cancellation_reason
        )
        expected = {
            "status": "success",
            "detail": f"Job of id {job_id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected

        job_in_db = _get_jobs_in_redis(redis_conn)[0]
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


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_another_users_job(client, redis_conn, jobs_folder, worker, job, mocker):
    """A user POST to '/jobs/{id}/cancel' for a job does is not for the current user errors out"""
    with client as client:
        cancellation_reason = "just testing"

        users = _create_many_users(client, raw_users=USERS[:2])
        raw_jobs = _get_raw_jobs(job, durations=[0.2])
        job_metadata_list = _get_job_submission_metadata(
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
        response = _cancel_job(
            client, user_id=other_user_id, job_id=job_id, reason=cancellation_reason
        )
        expected = {
            "detail": f"Job {job_id} not found",
        }
        got = response.json()

        assert response.status_code == 404
        assert got == expected

        job_in_db = _get_jobs_in_redis(redis_conn)[0]

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


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_admin_cancel_job(client, redis_conn, jobs_folder, worker, job, mocker):
    """A user POST to '/jobs/{id}/cancel' cancels the job of the job_id if the user is admin"""
    with client as client:
        cancellation_reason = "just testing"

        users = _create_many_users(client, raw_users=USERS[:2])
        raw_jobs = _get_raw_jobs(job, durations=[0.2])
        job_metadata_list = _get_job_submission_metadata(
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
        response = _cancel_job(
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

        job_in_db = _get_jobs_in_redis(redis_conn)[0]
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


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_cancel_job(
    client, redis_conn, worker, job, jobs_folder, mocker: MockerFixture
):
    """POST to `/jobs/{job_id}/cancel` raises 401 if not accessed through MSS"""
    with client as client:
        cancellation_reason = "just testing"

        users = _create_many_users(client, raw_users=USERS[:2])
        raw_jobs = _get_raw_jobs(job, durations=[0.2])
        job_metadata_list = _get_job_submission_metadata(
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

        # use a token used only for submitting jobs directly to the backend
        login_data = {"user_id": job_info["user_id"], "job_id": job_info["job_id"]}
        token, _ = _get_token(client, mocker, data=login_data)
        headers = _get_headers(token)
        response = client.post(url, json=payload, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_download_logfile(
    logfile_download_folder, client, redis_conn, worker, job, mocker, jobs_folder
):
    """GET to '/logfiles/{logfile_id}' downloads the given logfile"""
    # using context manager to ensure on_startup runs
    with client as client:
        _save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

        users = _create_many_users(client, raw_users=USERS[:1])

        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=[job], users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        job_info = job_metadata_list[0]

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

        response = client.get(f"/logfiles/{job["job_id"]}", headers=job_info["headers"])
        file_content = json.loads(response.content)
        assert response.status_code == 200
        assert file_content == job


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_logfile_for_non_existing_job(
    logfile_download_folder, client, redis_conn, worker, job, mocker
):
    """GET '/logfiles/{logfile_id}' for non-existing jobs fails with a 403"""
    # using context manager to ensure on_startup runs
    with client as client:
        job_id = job["job_id"]
        _save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

        user_id, _ = _create_user(client, USERS[0])
        login_data = {"user_id": user_id, "job_id": job_id}
        token, _ = _get_token(client, mocker=mocker, data=login_data)

        headers = _get_headers(token)

        response = client.get(f"/logfiles/{job_id}", headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": f"job {job_id} does not exist"}


@pytest.mark.timeout(240)
@pytest.mark.parametrize("client, redis_conn, worker, job", _SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_download_logfile(
    logfile_download_folder, client, redis_conn, worker, job, mocker, jobs_folder
):
    """Unauthenticated GET to '/logfiles/{logfile_id}' errors out"""
    # using context manager to ensure on_startup runs
    with client as client:
        _save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

        users = _create_many_users(client, raw_users=USERS[:1])

        # Creating this job metadata list first because it takes up a lot of time
        job_metadata_list = _get_job_submission_metadata(
            client, jobs=[job], users=users, mocker=mocker, jobs_folder=jobs_folder
        )

        # submit many jobs from many users
        _submit_multiple_jobs_v2(client, data=job_metadata_list)

        # no authentication
        response = client.get(f"/logfiles/{job["job_id"]}")
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}

        # wrong authentication
        headers = _get_headers("foo")
        response = client.get(f"/logfiles/{job["job_id"]}", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "not authenticated"}


def _cancel_job(
    client: "TestClient",
    user_id: str,
    job_id: str,
    reason: Optional[str] = None,
    is_admin: Optional[bool] = None,
) -> "Response":
    """Cancels the job on the REST API

    Args:
        client: the TestClient for running tests in
        user_id: the user ID for authentication
        job_id: the unique identifier of the job
        reason: the reason for cancelling the job
        is_admin: whether the user is an MSS admin

    Returns:
        the httpx.Response form the cancellation endpoint
    """
    headers = create_mss_headers(user_id, is_admin=is_admin)
    payload = {}

    if reason is not None:
        payload["reason"] = reason

    return client.post(f"/jobs/{job_id}/cancel", json=payload, headers=headers)


def _cancel_booking(client: "TestClient", token: str, booking_id: str) -> "Response":
    """Cancels the booking on the API

    Args:
        client: the fastapi TestClient for running tests
        token: the user token for authentication
        booking_id: the unique identifier of the booking

    Returns:
        the httpx.Response from the cancellation endpoint
    """
    headers = _get_headers(token)
    return client.post(f"/bookings/{booking_id}/cancel", headers=headers)


def _view_job(
    client: "TestClient", user_id: str, job_id: str, is_admin: Optional[bool] = None
) -> Tuple[Optional[Job], "Response"]:
    """Gets the job as viewed on the REST API

    Args:
        client: the TestClient for the running tests in
        user_id: the unique identifier of the user associated with this job
        job_id: the unique identifier of the job
        is_admin: whether the MSS user is admin

    Returns:
        the tuple of the job or None if no job and the httpx.Response from endpoint
    """
    headers = create_mss_headers(user_id, is_admin=is_admin)
    response = client.get(f"/jobs/{job_id}", headers=headers)
    try:
        job = Job.model_validate(response.json())
        assert response.status_code == 200
        return job, response
    except ValidationError:
        return None, response


def _view_booking_list(
    client: "TestClient",
    token: str,
    skip: int = 0,
    limit: Optional[int] = None,
) -> "Response":
    """Views the paginated list of bookings

    Args:
        client: the test client in which the tests run
        token: the user token for authentication
        skip: the number of records to skip
        limit: the maximum number of records to return

    Returns:
        the httpx.Response for the request
    """
    headers = _get_headers(token)
    params = {"skip": skip}
    if isinstance(limit, int):
        params["limit"] = limit

    return client.get("/bookings", headers=headers, params=params)


def _view_user_list(
    client: "TestClient",
    token: str,
    skip: int = 0,
    limit: Optional[int] = None,
) -> "Response":
    """Views the pagination list of users on the REST API

    Args:
        client: the fastapi TestClient for testing the API
        token: the user token for authentication
        skip: the number of records to skip
        limit: the maximum number of records to return

    Returns:
        the httpx.Response
    """
    params = {"skip": skip}
    if isinstance(limit, int):
        params["limit"] = limit

    headers = _get_headers(token)
    return client.get("/users", headers=headers, params=params)


def _view_own_profile(client: "TestClient", token: str) -> "Response":
    """Views profile of the current user on the REST API

    Args:
        client: the fastapi TestClient for testing the app
        token: the user token for authentication

    Returns:
        the REST httpx.Response
    """
    return client.get("/me", headers=_get_headers(token))


def _delete_own_profile(client: "TestClient", token: str) -> "Response":
    """Deletes profile of the current user

    Args:
        client: the fastapi TextClient in which the tests run
        token: the user token for authentication

    Returns:
        the httpx.Response
    """
    return client.delete("/me", headers=_get_headers(token))


def _get_headers(token: str) -> Dict[str, Any]:
    """Gets the headers corresponding to the given token

    Args:
        token: the token of the user

    Returns:
        the dict of headers
    """
    return {"Authorization": f"Bearer {token}"}


def _to_booking_payload(booking_info: "_BasicBookingInfo") -> "_BookingPayload":
    """Converts the booking info to payload for creation of a new booking

    Args:
        booking_info: the basic booking info containing starts_in, duration

    Returns:
        the payload for creating a booking
    """
    starts_in = timedelta(seconds=booking_info["starts_in"])
    duration = timedelta(seconds=booking_info["duration"])

    current_timestamp = datetime.now(timezone.utc)
    start_utc = current_timestamp + starts_in
    end_utc = start_utc + duration
    return {
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
    }


def _get_raw_jobs(job_template: dict, durations: List[float]) -> List[dict]:
    """Generates a list of jobs given a job template and durations

    Args:
        job_template: the template all jobs adhere to
        durations: the durations corresponding to each job

    Returns:
        List of jobs, one job per entry in the list of durations
    """
    raw_jobs = []
    for duration in durations:
        new_job = copy.deepcopy(job_template)
        new_job["job_id"] = f"{uuid4()}"
        new_job["params"]["qobj"]["header"]["test_duration"] = duration
        raw_jobs.append(new_job)
    return raw_jobs


def _remove_user(client: "TestClient", token: str, user_id: str) -> "Response":
    """Delete the user of the given user ID

    Args:
        client: the fastapi TestClient for running tests
        token: the user token for authentication
        user_id: the unique identifier of the given user

    Returns:
        the httpx.Response
    """
    headers = _get_headers(token)
    return client.delete(f"/users/{user_id}", headers=headers)


def _paginate(
    data: List[T], skip: int = 0, limit: Optional[int] = None
) -> "_PaginatedList[T]":
    """Paginates the data basing on the skip and the limit params

    Args:
        skip: the number of records to skip
        limit: the maximum number of records to return

    Returns:
        list of the data sliced according to the pagination info
    """
    slice_limit = limit
    if isinstance(slice_limit, int):
        slice_limit += skip
    return {"skip": skip, "limit": limit, "data": data[skip:slice_limit]}


def _drop_microsec(timestamp: datetime) -> datetime:
    """Drops the microseconds on the timestamp

    Args:
        timestamp: the datetime which may or may not have microseconds

    Returns:
        the datetime but without microseconds
    """
    return timestamp.replace(microsecond=0)


def _get_jobs_in_redis(redis_client: Redis) -> List[Job]:
    """Gets the jobs that are in redis

    Args:
        redis_client: the client for accessing redis

    Returns:
        the list of jobs in redis
    """
    raw_jobs_in_redis = redis_client.hgetall(JOBS_HASH_NAME)
    return [Job.model_validate_json(item) for item in raw_jobs_in_redis.values()]


def _submit_multiple_jobs_v2(
    client: "TestClient", data: List["_JobSubmissionMetadata"] = None
) -> List[str]:
    """Submits multiple jobs given the job metadata list.

    Args:
        client: the fastapi TestClient for testing
        data: list of the job metadata to use for submission

    Returns:
        the list of job_ids of the created jobs
    """
    job_ids_in_fifo = []

    for job_info in data:
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        actual_job = response.json()
        job_ids_in_fifo.append(actual_job["job_id"])

        assert actual_job["user_id"] == job_info["user_id"]
        assert actual_job["job_id"] == job_info["job_id"]

    return job_ids_in_fifo


def _get_job_submission_metadata(
    client: "TestClient",
    jobs: List[dict],
    users: List[dict],
    mocker: MockerFixture,
    jobs_folder: Path,
) -> List["_JobSubmissionMetadata"]:
    """Generates a list of job submission metadata to use when submitting jobs

    Metadata includes the token to use, the user_id, the file path
    This operation is quite expensive

    Args:
        client: the test client to run the tests on
        jobs: list of jobs for which the map is constructed
        users: the list of users to use to submit the jobs in a round-robin style
        mocker: the mocker for intercepting the generation of the user token
        jobs_folder: the folder in which the job files are to be saved temporarily before submission

    Returns:
        a list of job submission metadata
    """
    num_of_users = len(users)
    results = []
    for idx, job_info in enumerate(jobs):
        curr_user = users[idx % num_of_users]
        job_file_path = _save_job_file(folder=jobs_folder, job=job_info)
        user_id = curr_user["id"]
        job_id = job_info["job_id"]

        token, _ = _get_token(
            client, mocker, data={"user_id": user_id, "job_id": job_id}
        )

        results.append(
            {
                "job_id": job_id,
                "user_id": user_id,
                "file_path": job_file_path,
                "token": token,
                "headers": _get_headers(token),
            }
        )

    return results


def _submit_multiple_jobs(
    client: "TestClient",
    user_id_token_map: Dict[str, str],
    is_job_id_specified: bool = True,
    raw_jobs: List[Dict[str, Any]] = tuple(JOBS),
) -> List[str]:
    """Submits multiple jobs one or more per user in the user_id_token_map.

    It attempts to spread the jobs evenly across the users, while
    maintaining the order i.e. the first user is matched with the first job,
    the second with the second etc. until all the users are used up then the cycle
    is restarted from the first user.

    Args:
        client: the fastapi TestClient for testing
        user_id_token_map: the dict of <user_id>:<token> for use when submitting jobs
        is_job_id_specified: whether the job id is specified when submitting the jobs
        raw_jobs: the list of raw job data; default = JOBS

    Returns:
        the list of job_ids of the created jobs
    """
    job_ids_in_fifo = []
    user_id_token_pairs = list(user_id_token_map.items())
    num_of_pairs = len(user_id_token_pairs)

    for idx, job_info in enumerate(raw_jobs):
        (user_id, token) = user_id_token_pairs[idx % num_of_pairs]
        headers = _get_headers(token)
        data = {
            "type": job_info["type"],
            "test_duration": job_info["test_duration"],
        }
        if is_job_id_specified:
            data["job_id"] = job_info["job_id"]

        response = client.post("/jobs", headers=headers, json=data)
        actual_job = response.json()
        job_ids_in_fifo.append(actual_job["job_id"])

        assert actual_job["job_id"]
        assert actual_job["user_id"] == user_id
        assert actual_job["type"] == job_info["type"]
        assert actual_job["test_duration"] == job_info["test_duration"]

        if is_job_id_specified:
            assert actual_job["job_id"] == job_info["job_id"]

    return job_ids_in_fifo


def _create_user_token_map(
    client: "TestClient",
    mocker: MockerFixture,
    raw_users: List[Dict[str, Any]] = tuple(USERS),
) -> Dict[str, str]:
    """Creates a map of <user id>:<token>

    Args:
        client: the fastapi test client
        mocker: the MockerFixture for mocking JWT calls etc
        raw_users: the list of raw user data

    Returns:
        dictionary of user_id: token
    """
    user_id_token_map: Dict[str, str] = {}
    for user in raw_users:
        user_id, _ = _create_user(client, user=user)
        # FIXME: this is wrong
        token, _ = _get_token(client, mocker=mocker, data=user)
        user_id_token_map[user_id] = token
    return user_id_token_map


def _create_many_users(
    client: "TestClient",
    raw_users: List[Dict[str, Any]] = tuple(USERS),
) -> List[Dict[str, Any]]:
    """Creates a list of users, returning them as dicts

    Args:
        client: the fastapi test client
        raw_users: the list of raw user data

    Returns:
        dictionary of user_id: token
    """
    result: List[Dict[str, Any]] = []
    for user in raw_users:
        user_id, resp = _create_user(client, user=user)
        result.append(resp.json())
    return result


def _create_booking_v2(
    client: "TestClient", user_id: str, booking: "_BasicBookingInfo"
) -> Tuple["_BasicBookingInfo", "Response"]:
    """Creates the booking and returns the actual booking details

    Actual booking details include actual duration and actual delay

    Args:
        client: the fastapi TestClient used for testing
        user_id: the id of the user
        booking: the basic booking info

    Returns:
        the tuple of the actual basic info of the created booking and the httpx.Response
    """
    headers = create_mss_headers(user_id)
    current_timestamp = datetime.now(timezone.utc)
    data = _to_booking_payload(booking)
    response = client.post("/bookings", headers=headers, json=data)
    json_response = response.json()

    start_utc = json_response["start_utc"]
    start_utc_datetime = datetime.fromisoformat(start_utc)
    end_utc = json_response["end_utc"]
    end_utc_datetime = datetime.fromisoformat(end_utc)

    actual_duration = (end_utc_datetime - start_utc_datetime).total_seconds()
    actual_delay = (start_utc_datetime - current_timestamp).total_seconds()
    # round off delay just to shave off the milliseconds lost during execution
    actual_delay = int(actual_delay)

    return {
        "duration": actual_duration,
        "starts_in": actual_delay,
    }, response


def _create_booking(
    client: "TestClient", token: str, booking: "_BasicBookingInfo"
) -> Tuple["_BasicBookingInfo", "Response"]:
    """Creates the booking and returns the actual booking details

    Actual booking details include actual duration and actual delay

    Args:
        client: the fastapi TestClient used for testing
        token: the JWT token for authenticating the user
        booking: the basic booking info

    Returns:
        the tuple of the actual basic info of the created booking and the httpx.Response
    """
    headers = _get_headers(token)
    current_timestamp = datetime.now(timezone.utc)
    data = _to_booking_payload(booking)
    response = client.post("/bookings", headers=headers, json=data)
    json_response = response.json()

    start_utc = json_response["start_utc"]
    start_utc_datetime = datetime.fromisoformat(start_utc)
    end_utc = json_response["end_utc"]
    end_utc_datetime = datetime.fromisoformat(end_utc)

    actual_duration = (end_utc_datetime - start_utc_datetime).total_seconds()
    actual_delay = (start_utc_datetime - current_timestamp).total_seconds()
    # round off delay just to shave off the milliseconds lost during execution
    actual_delay = int(actual_delay)

    return {
        "duration": actual_duration,
        "starts_in": actual_delay,
    }, response


def _create_root_user(
    client: "TestClient", user: dict
) -> Tuple[Optional[str], "Response"]:
    """Creates a root user in the REST API

    Args:
        client: the fastapi TestClient for testing the REST API
        user: the user info for creating the user

    Returns:
        the tuple of user_id if successful or None if not, and httpx.Response
    """
    response = client.post("/root-user", json=user)

    try:
        json_response = response.json()
        return json_response["id"], response
    except KeyError:
        return None, response


def _create_user(client: "TestClient", user: dict) -> Tuple[str, "Response"]:
    """Creates a user and returns their ID and the httpx.Response

    Args:
        client: the fastapi TestClient used for testing
        user: the user info for creating the user

    Returns:
        the tuple of id of the user and the result
    """
    headers = create_mss_headers(is_admin=True)
    response = client.post("/users", json=user, headers=headers)
    json_response = response.json()
    return json_response["id"], response


def _get_token(
    client: "TestClient", mocker: MockerFixture, data: "_LoginDetails"
) -> Tuple[str, "Response"]:
    """Mimics logging in via MSS and returns the user's JWT token and the httpx.Response

    Args:
        client: the fastapi TestClient for testing the app
        mocker: the pytest mocker fixture for spying on functions
        data: the info for signing in

    Returns:
        the tuple of the JWT of the user and the httpx.Response
    """
    import jwt

    jwt_encode_spy = mocker.spy(jwt, "encode")

    headers = create_mss_headers(user_id=data["user_id"])
    response = client.post("/token", json=data, headers=headers)
    expected_token = jwt_encode_spy.spy_return
    mocker.stop(jwt_encode_spy)

    return expected_token, response


def _save_job_file(folder: Path, job: Dict[str, Any], ext: str = ".json") -> Path:
    """Saves the given job to a file and returns the Path

    It uses 'dummy' as a default job id

    Args:
        folder: the folder to save the job in
        job: the job to save

    Returns:
        the path where the job was saved
    """
    job_id = job.get("job_id", "dummy")
    file_path = folder / f"{job_id}{ext}"

    with open(file_path, "w") as file:
        json.dump(job, file)

    return file_path


def _job_results_match(results: List[str], expected_min_counts: Dict[str, int]):
    """Matches job results, keeping in mind that the results are probabilistic

    The states are written in hex '0x0', '0x1', '0x2', '0x3' i.e. 00, 01, 10, 11

    Args:
        results: the results obtained
        expected_min_counts: the expected counts minimum counts for each state

    Returns:
        True if they match, False if they don't
    """
    return all([results.count(k) >= v for k, v in expected_min_counts.items()])


class _PaginatedList(TypedDict, Generic[T]):
    """The type for paginated responses"""

    skip: int
    limit: Optional[int]
    data: List[T]


class _BookingPayload(TypedDict):
    """The payload for creation of a booking"""

    start_utc: str
    end_utc: str


class _LoginDetails(TypedDict):
    """Details used to log in"""

    user_id: str
    job_id: str


class _JobSubmissionMetadata(TypedDict):
    job_id: str
    token: str
    user_id: str
    file_path: Path
    headers: dict
