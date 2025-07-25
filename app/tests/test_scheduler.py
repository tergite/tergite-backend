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

import time
from datetime import datetime, timedelta, timezone
from itertools import islice
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

from app.services.booking.models import Booking
from app.tests.conftest import (
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
from app.tests.utils.env import TEST_MAX_SLOTS_PER_DAY
from app.utils.queues.dtos import Job, JobStatus

if TYPE_CHECKING:
    from httpx import Response
    from starlette.testclient import TestClient

T = TypeVar("T")


@pytest.mark.parametrize("user", USERS)
def test_create_user(rest_client, user):
    """POST to '/users' should create the user in the REST API

        curl -L 'http://127.0.0.1:5000/users' \
        -H 'Content-Type: application/json' \
        --data-raw '{
            "name": "John Doe",
            "email": "johndoe@example.com",
            "password": "some-password-for-john"
        }'
    """
    with rest_client as client:
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
def test_view_profile(rest_client, user, mocker: MockerFixture):
    """GET '/me' should show the user profile in the REST API

    curl -L 'http://127.0.0.1:5000/me' \
    -H 'Authorization: Bearer access-token-for-john'
    """
    with rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _login_user(client, mocker=mocker, user=user)

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


def test_create_root_user(rest_client):
    """POST to '/root-user' creates the root user only once per app instance

        curl -L 'http://127.0.0.1:5000/root-user' \
        -H 'Content-Type: application/json' \
        --data-raw '{
            "name": "Peninah Doe",
            "email": "peninahdoe@example.com",
            "password": "some-password-for-peninah"
        }'
    """
    with rest_client as client:
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
    rest_client, pagination: "_PaginationInfo", mocker: MockerFixture
):
    """GET '/users' should show to an admin the paginated list of user profiles

    curl -L 'http://127.0.0.1:5000/users?skip=1&limit=10' \
    -H 'Authorization: Bearer access-token-for-john'
    """
    with rest_client as client:
        # create admin user and token
        admin_user_data, *other_user_data = USERS
        admin_user_id, _ = _create_root_user(client, user=admin_user_data)
        admin_token, _ = _login_user(client, mocker=mocker, user=admin_user_data)

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
def test_delete_profile(rest_client, rq_worker, redis_client, mocker: MockerFixture):
    """DELETE '/me' should delete their own user profile

    curl -L -X DELETE 'http://127.0.0.1:5000/me' \
    -H 'Authorization: Bearer access-token-for-john'
    """
    with rest_client as client:
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
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=False,
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
def test_admin_remove_user(rest_client, rq_worker, redis_client, mocker: MockerFixture):
    """DELETE '/users/{user_id}' by ddmin removes the user, and their bookings and jobs

    curl -L -X DELETE 'http://127.0.0.1:5000/users/janes-user-id' \
    -H 'Authorization: Bearer access-token-for-john'
    """
    with rest_client as client:
        # create admin user and token
        admin_user_data, *other_user_data = USERS
        admin_user_id, _ = _create_root_user(client, user=admin_user_data)
        admin_token, _ = _login_user(client, mocker=mocker, user=admin_user_data)

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
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """Non admin cannot remove the profile of any user"""
    with rest_client as client:
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
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=False,
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
def test_login(rest_client, user, mocker: MockerFixture):
    """POST '/login' should return the JWT token for the user

    curl -L 'http://127.0.0.1:5000/login' \
    -H 'Content-Type: application/json' \
    --data-raw '{
        "email": "johndoe@example.com",
        "password": "some-password-for-john"
    }'
    """
    with rest_client as client:
        _create_user(client, user=user)
        token, response = _login_user(client, mocker=mocker, user=user)

        assert response.status_code == 200
        assert response.json() == {"access_token": token, "token_type": "bearer"}


@pytest.mark.parametrize("user,booking", VALID_CREATE_BOOKINGS_PARAMS)
def test_create_booking(
    rest_client, user, booking: "_BasicBookingInfo", mocker: MockerFixture
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
    with rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _login_user(client, mocker=mocker, user=user)

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
    rest_client, user, booking: "_BasicBookingInfo", mocker: MockerFixture
):
    """Should return an error message if an attempt to create an invalid booking is made

    e.g. start_utc in the past
    """
    with rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _login_user(client, mocker=mocker, user=user)
        data = _to_booking_payload(booking)

        # create booking
        headers = _get_headers(token)
        response = client.post("/bookings", headers=headers, json=data)
        detail = f"{response.json()["detail"]}"

        assert response.status_code == 422
        assert booking["error_message"] in detail


@pytest.mark.parametrize("user,booking", VALID_CREATE_BOOKINGS_PARAMS)
def test_create_conflicting_booking(
    rest_client, user, booking: "_BasicBookingInfo", mocker: MockerFixture
):
    """Should return an error message when creating a booking overlapping with another"""
    with rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _login_user(client, mocker=mocker, user=user)

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
def test_create_too_many_booking(rest_client, user, mocker: MockerFixture):
    """Returns error message when creating a booking when number of bookings in a period for user are maxed out."""
    with rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _login_user(client, mocker=mocker, user=user)

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


def test_create_booking_invalid_token(rest_client, rq_worker, redis_client):
    """Creating booking with invalid tokens results in an error"""
    with rest_client as client:
        headers = _get_headers("token")
        data = _to_booking_payload(VALID_BOOKINGS[0])

        response = client.post("/bookings", headers=headers, json=data)
        json_response = response.json()

        assert response.status_code == 401
        assert "not authenticated" in json_response["detail"]


def test_create_booking_without_token(rest_client, rq_worker, redis_client):
    """Creating booking without a token results in an error"""
    with rest_client as client:
        data = _to_booking_payload(VALID_BOOKINGS[0])

        response = client.post("/bookings", data=data)
        json_response = response.json()

        assert response.status_code == 401
        assert json_response["detail"] == "Not authenticated"


@pytest.mark.parametrize("pagination", PAGINATION)
def test_view_bookings(
    rest_client, mocker: MockerFixture, pagination: "_PaginationInfo"
):
    """GET "/bookings" shows paginated list of all available bookings"""
    with rest_client as client:
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


def test_view_bookings_invalid_token(rest_client, rq_worker, redis_client):
    """Viewing bookings with invalid tokens results in an error"""
    with rest_client as client:
        response = _view_booking_list(client, token="token")
        json_response = response.json()

        assert response.status_code == 401
        assert "not authenticated" in json_response["detail"]


def test_view_bookings_without_token(rest_client, rq_worker, redis_client):
    """Viewing bookings without a token results in an error"""
    with rest_client as client:
        response = client.get("/bookings")
        json_response = response.json()

        assert response.status_code == 401
        assert json_response["detail"] == "Not authenticated"


def test_submit_jobs_no_booking(
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/jobs' when there is no booking should run the jobs in FIFO (first in, first out)"""
    with rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)

        # submit many jobs from many users
        job_ids_in_fifo = _submit_multiple_jobs(
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=True,
        )

        # Run the queue
        rq_worker.work(burst=True)
        jobs_in_redis = _get_jobs_in_redis(redis_client)
        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)

        # Assert that they are all complete and their timestamp of completion is in FIFO order
        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert [v.job_id for v in jobs_in_redis] == job_ids_in_fifo


def test_submit_jobs_no_booking_no_job_id(
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST jobs without job_id to '/jobs' when there is no booking runs them in FIFO (first in, first out)"""
    with rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)

        # submit many jobs from many users
        job_ids_in_fifo = _submit_multiple_jobs(
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=False,
        )

        # Run the queue
        rq_worker.work(burst=True)
        jobs_in_redis = _get_jobs_in_redis(redis_client)

        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        # Assert that they are all complete and their timestamp of completion is in FIFO order
        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert [v.job_id for v in jobs_in_redis] == job_ids_in_fifo


@pytest.mark.timeout(180)
def test_submit_jobs_in_active_booking(
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/jobs' when there is an active booking runs the booker jobs first then runs the other jobs after booking."""
    with rest_client as client:
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

        # submit many jobs from many users when booking starts
        _submit_multiple_jobs(
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=False,
        )

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = rq_worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            rq_worker.work(burst=True, with_scheduler=True)

        jobs_in_redis = _get_jobs_in_redis(redis_client)

        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        booker_jobs = [job for job in jobs_in_redis if job.user_id == booker_user_id]
        non_booker_jobs = [
            job for job in jobs_in_redis if job.user_id != booker_user_id
        ]
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


@pytest.mark.timeout(180)
def test_submit_jobs_in_idle_booking(
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/jobs' when there is an idle booking runs the non booker jobs even during the booking."""
    with rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker_user_id = next(islice(user_id_token_map, 0, 1))
        booker_token = user_id_token_map[booker_user_id]
        booking_info = {"duration": 5, "starts_in": 0}
        _, response = _create_booking(client, token=booker_token, booking=booking_info)
        booking = Booking.model_validate(response.json())

        # submit many jobs from many users when booking starts
        _submit_multiple_jobs(
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=False,
        )

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = rq_worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            rq_worker.work(burst=True, with_scheduler=True)

        jobs_in_redis = _get_jobs_in_redis(redis_client)

        jobs_in_redis.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        booker_jobs = [job for job in jobs_in_redis if job.user_id == booker_user_id]
        non_booker_jobs = [
            job for job in jobs_in_redis if job.user_id != booker_user_id
        ]
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
        assert first_non_booker_job_start < booking_end_timestamp


@pytest.mark.timeout(180)
def test_submit_jobs_in_idle_booking_before_another(
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """When there is an idle booking and another is next, only non booker jobs, short enough to fit in between, run."""
    with rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker_user_id = next(islice(user_id_token_map, 0, 1))
        booker_token = user_id_token_map[booker_user_id]

        # create first booking
        first_booking_info = {"duration": 2, "starts_in": 0}
        _, result = _create_booking(
            client, token=booker_token, booking=first_booking_info
        )

        # create second booking
        second_booking_info = {"duration": 3, "starts_in": 3}
        _, response = _create_booking(
            client, token=booker_token, booking=second_booking_info
        )
        next_slot = Booking.model_validate(response.json())

        durations = [0.2, 3, 4, 1.1, 2.9, 0.4, 0.15]
        raw_jobs = [
            {"type": "test", "test_duration": duration, "job_id": f"{uuid4()}"}
            for duration in durations
        ]

        # submit many jobs from many users when booking starts
        _submit_multiple_jobs(
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=True,
            raw_jobs=raw_jobs,
        )

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = rq_worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            rq_worker.work(burst=True, with_scheduler=True)

        jobs_in_db = _get_jobs_in_redis(redis_client)
        booker_jobs = [v for v in jobs_in_db if v.user_id == booker_user_id]
        non_booker_jobs = [v for v in jobs_in_db if v.user_id != booker_user_id]

        booker_jobs.sort(key=lambda v: v.test_duration)
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

        # 1.1, 0.4, 0.15 total to 1.65, plus 1.0 max idle time, equal to  which is less than (3 - 1 - 0.2) = 1.8
        # where 1 is the max idle time of booking
        #
        # first and fifth jobs are submitted by the owner of the booking;
        # the fourth is too long for the current booking so it fails immediately.
        non_booker_pre_2dn_slot_durations = (1.1, 0.4, 0.15)
        booker_job_durations = (0.2, 2.9)
        expected_non_booker_job_ids_pre_2nd_slot = [
            v["job_id"]
            for v in raw_jobs
            if v["test_duration"] in non_booker_pre_2dn_slot_durations
        ]
        expected_non_booker_job_ids_post_2nd_slot = [
            v["job_id"]
            for v in raw_jobs
            if v["test_duration"]
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


@pytest.mark.timeout(180)
def test_submit_long_jobs_before_booking(
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST long jobs to '/jobs' before booking starts waitlists them but runs the shorter ones"""
    with rest_client as client:
        # create many users and their tokens
        user_id_token_map = _create_user_token_map(client, mocker=mocker)

        # create booking
        # first user; thus first job (short duration) belongs to them
        booker_user_id = next(islice(user_id_token_map, 0, 1))
        booker_token = user_id_token_map[booker_user_id]

        # create job data
        durations = [0.4, 3, 4, 1.1, 2.9, 0.3, 0.1]
        raw_jobs = [
            {"type": "test", "test_duration": duration, "job_id": f"{uuid4()}"}
            for duration in durations
        ]

        booking_info = {"duration": 3, "starts_in": 2.2}
        _create_booking(client, token=booker_token, booking=booking_info)

        # submit many jobs from many users when booking starts
        _submit_multiple_jobs(
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=True,
            raw_jobs=raw_jobs,
        )

        # Run the queue; try to wait for waitlist to transfer things to execution queue
        general_queue = rq_worker.queues[0]
        while general_queue.scheduled_job_registry.count > 0:
            rq_worker.work(burst=True, with_scheduler=True)

        jobs_in_db = _get_jobs_in_redis(redis_client)
        jobs_in_db.sort(key=lambda v: v.start_utc)
        job_estimated_durations = [v.estimated_duration for v in jobs_in_db]

        assert all([job.status == JobStatus.SUCCESSFUL for job in jobs_in_db])
        # the small-enough first (0.4, 1.1, 0.3, 0.1),
        # then the bigger ones (3.0, 4.0, 2.9) in original order
        assert job_estimated_durations == [0.4, 1.1, 0.3, 0.1, 3.0, 4.0, 2.9]


def test_submit_jobs_invalid_token(rest_client, rq_worker, redis_client):
    """POST '/jobs' with invalid tokens results in an error"""
    with rest_client as client:
        headers = _get_headers("token")
        response = client.post("/jobs", headers=headers, json=JOBS[0])
        json_response = response.json()

        assert response.status_code == 401
        assert "not authenticated" in json_response["detail"]


def test_submit_jobs_without_token(rest_client, rq_worker, redis_client):
    """POST to '/jobs' jobs without a token results in an error"""
    with rest_client as client:
        response = client.post("/jobs", json=JOBS[0])
        json_response = response.json()

        assert response.status_code == 401
        assert json_response["detail"] == "Not authenticated"


@pytest.mark.timeout(180)
def test_cancel_future_booking(
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/booking/{id}/cancel' for a future booking, deletes the booking and allows jobs to run without it."""
    with rest_client as client:
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
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=False,
        )

        # Run the queue
        rq_worker.work(burst=True)

        jobs_in_redis = _get_jobs_in_redis(redis_client)

        jobs_in_redis.sort(key=lambda v: v.start_utc)
        job_ids = [job.job_id for job in jobs_in_redis]

        assert all([v.status == JobStatus.SUCCESSFUL for v in jobs_in_redis])
        assert job_ids == expected_job_ids


def test_admin_cancel_future_booking(rest_client, mocker: MockerFixture):
    """Admin POST '/bookings/{id}/cancel' cancels any user's future booking"""
    with rest_client as client:
        # create admin user and token
        admin_user_data, *other_user_data = USERS
        admin_user_id, _ = _create_root_user(client, user=admin_user_data)
        admin_token, _ = _login_user(client, mocker=mocker, user=admin_user_data)

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
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/bookings/{id}/cancel' for an active booking fails."""
    with rest_client as client:
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
            client,
            user_id_token_map=user_id_token_map,
            is_job_id_specified=False,
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
    rest_client, rq_worker, redis_client, mocker: MockerFixture
):
    """POST '/bookings/{id}/cancel' for a completed booking fails."""
    with rest_client as client:
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


@pytest.mark.parametrize("user", USERS)
def test_view_job(rest_client, user, rq_worker, redis_client, mocker: MockerFixture):
    """GET '/jobs/{job_id}' by a user can show the job for the job_id if job belongs to them"""
    with rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _login_user(client, mocker=mocker, user=user)
        job_info = JOBS[0]
        job_id = job_info["job_id"]

        # submit job
        headers = _get_headers(token)
        response = client.post("/jobs", headers=headers, json=job_info)
        expected_job = Job.model_validate(response.json())

        pending_job, _ = _view_job(client, token=token, job_id=job_id)
        assert pending_job == expected_job

        # Run the queue to run the job and see that it is updated
        rq_worker.work(burst=True)

        complete_job, _ = _view_job(client, token=token, job_id=job_id)
        assert complete_job.status == JobStatus.SUCCESSFUL
        assert complete_job.actual_duration is not None


@pytest.mark.parametrize("user", USERS)
def test_cancel_job(rest_client, user, rq_worker, redis_client, mocker: MockerFixture):
    """A user POST to '/jobs/{id}/cancel' cancels the job of the job_id if the job belongs to them"""
    with rest_client as client:
        user_id, _ = _create_user(client, user=user)
        token, _ = _login_user(client, mocker=mocker, user=user)
        job_info = JOBS[0]
        job_id = job_info["job_id"]

        # submit job
        headers = _get_headers(token)
        response = client.post("/jobs", headers=headers, json=job_info)
        original_job = Job.model_validate(response.json())

        response = _cancel_job(client, token=token, job_id=job_id)
        expected = {
            "status": "success",
            "detail": f"Booking of id {job_id} cancelled",
        }
        got = response.json()

        assert response.status_code == 200
        assert got == expected

        # Run the queue to run the job and see that it is updated
        rq_worker.work(burst=True)

        complete_job, response = _view_job(client, token=token, job_id=job_id)
        expected_updates = {
            "status": JobStatus.CANCELLED,
            "failure_reason": "Cancelled by a user",
        }
        expected_job = original_job.model_copy(update=expected_updates)
        assert response.status_code == 200
        assert complete_job == expected_job


def _cancel_job(client: "TestClient", token: str, job_id: str) -> "Response":
    """Cancels the job on the REST API

    Args:
        client: the TestClient for running tests in
        token: the user token for authentication
        job_id: the unique identifier of the job

    Returns:
        the httpx.Response form the cancellation endpoint
    """
    headers = _get_headers(token)
    return client.post(f"/jobs/{job_id}/cancel", headers=headers)


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
    client: "TestClient", token: str, job_id: str
) -> Tuple[Optional[Job], "Response"]:
    """Gets the job as viewed on the REST API

    Args:
        client: the TestClient for the running tests in
        token: the user token for authentication
        job_id: the unique identifier of the job

    Returns:
        the tuple of the job or None if no job and the httpx.Response from endpoint
    """
    headers = _get_headers(token)
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
        token, _ = _login_user(client, mocker=mocker, user=user)
        user_id_token_map[user_id] = token
    return user_id_token_map


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
    response = client.post("/users", json=user)
    json_response = response.json()
    return json_response["id"], response


def _login_user(
    client: "TestClient", mocker: MockerFixture, user: dict
) -> Tuple[str, "Response"]:
    """Logs in the user and returns the user's JWT token and the httpx.Response

    Args:
        client: the fastapi TestClient for testing the app
        mocker: the pytest mocker fixture for spying on functions
        user: the user info for signing in

    Returns:
        the tuple of the JWT of the user and the httpx.Response
    """
    import jwt

    jwt_encode_spy = mocker.spy(jwt, "encode")

    response = client.post("/login", json=user)
    expected_token = jwt_encode_spy.spy_return
    mocker.stop(jwt_encode_spy)

    return expected_token, response


class _PaginatedList(TypedDict, Generic[T]):
    """The type for paginated responses"""

    skip: int
    limit: Optional[int]
    data: List[T]


class _BookingPayload(TypedDict):
    """The payload for creation of a booking"""

    start_utc: str
    end_utc: str
