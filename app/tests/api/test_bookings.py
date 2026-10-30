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
"""Tests for the API's booking routes"""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pytest_mock import MockerFixture

from app.libs.queues.dtos import JobStatus
from app.services.booking import Booking
from app.tests.utils.api import (
    create_mss_headers,
    get_bearer_headers,
)
from app.tests.utils.bookings import (
    cancel_booking,
    create_booking,
    post_booking,
    to_booking_payload,
    view_booking_list,
)
from app.tests.utils.env import (
    TEST_MAX_IDLE_TIME,
    TEST_MAX_SLOTS_PER_DAY,
    TEST_MAX_TIME_SLOT_LENGTH,
    TEST_MIN_TIME_SLOT_LENGTH,
)
from app.tests.utils.jobs import (
    get_job_submission_metadata,
    get_raw_jobs,
    get_stored_jobs,
    submit_jobs,
)
from app.tests.utils.params import (
    CLIENT_AND_WORKER_TUPLES,
    FASTAPI_CLIENTS,
    INVALID_CREATE_BOOKINGS_PARAMS,
    SIMPLE_UPLOAD_JOB_PARAMS,
    USERS,
    VALID_BOOKINGS,
    VALID_CREATE_BOOKINGS_PARAMS,
    VIEW_MANY_PARAMS,
    BasicBookingInfo,
    PaginationInfo,
)
from app.tests.utils.records import order_by, paginate
from app.tests.utils.users import (
    create_many_users,
    create_user,
)
from app.tests.utils.workers import wait_on_worker


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


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
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


@pytest.mark.parametrize("pagination,client", VIEW_MANY_PARAMS)
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


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
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


@pytest.mark.parametrize("client, redis_conn, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
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
        worker.run(is_burst=True)

        stored_jobs = get_stored_jobs()

        stored_jobs.sort(key=lambda v: v.start_utc)
        job_ids = [job.job_id for job in stored_jobs]

        assert all([v.status == JobStatus.SUCCESSFUL for v in stored_jobs])
        assert job_ids == expected_job_ids


@pytest.mark.parametrize("client, redis_conn, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
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
        monkeypatch.setattr(device_settings, "current_date", current_date)
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
    "client, redis_conn, worker, job", SIMPLE_UPLOAD_JOB_PARAMS[:-1]
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
        wait_on_worker(worker, clock=fake_clock)

        stored_jobs = get_stored_jobs()

        stored_jobs.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        booker_jobs = [job for job in stored_jobs if job.user_id == booker_user_id]
        last_booker_job = booker_jobs[-1]

        # Assert that they are all complete and their booker jobs started before booking end_utc
        # while non booker jobs started after end_utc
        # Note: Enqueue_at ignores microseconds
        booking_end_timestamp = booking.end_utc
        last_booker_job_start = last_booker_job.timestamps.execution.start_timestamp

        assert all([v.status == JobStatus.SUCCESSFUL for v in stored_jobs])
        assert last_booker_job_start < booking_end_timestamp


@pytest.mark.parametrize("client, redis_conn, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
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
