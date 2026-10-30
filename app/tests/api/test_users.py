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
"""Tests for the API's user and token routes"""
from uuid import uuid4

import jwt
import pytest
from pytest_mock import MockerFixture

from app.libs.queues.dtos import JobStatus
from app.tests.utils.api import (
    create_mss_headers,
    decrypt_access_token,
    get_token,
)
from app.tests.utils.bookings import (
    create_booking,
)
from app.tests.utils.env import (
    TEST_JWT_SECRET,
    TEST_MAX_SLOTS_PER_DAY,
)
from app.tests.utils.jobs import (
    get_job_submission_metadata,
    get_raw_jobs,
    get_stored_jobs,
    submit_jobs,
)
from app.tests.utils.params import (
    FASTAPI_CLIENTS,
    SIMPLE_UPLOAD_JOB_PARAMS,
    USERS,
    VALID_BOOKINGS,
    VIEW_MANY_PARAMS,
    PaginationInfo,
)
from app.tests.utils.records import paginate
from app.tests.utils.users import (
    create_many_users,
    create_user,
    delete_own_profile,
    remove_user,
    view_own_profile,
    view_user_list,
)
from app.tests.utils.workers import wait_on_worker


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


@pytest.mark.parametrize("pagination, client", VIEW_MANY_PARAMS)
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_delete_profile(
    client, worker, job, jobs_folder, mocker: MockerFixture, fake_clock
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
        wait_on_worker(worker, clock=fake_clock)

        stored_jobs = get_stored_jobs()

        deleted_user_jobs = [job for job in stored_jobs if job.user_id == curr_user_id]
        other_user_jobs = [job for job in stored_jobs if job.user_id != curr_user_id]
        cancellation_reason = "Cancelled on user deletion"

        assert all([v.status == JobStatus.CANCELLED for v in deleted_user_jobs])
        assert all(
            [v.cancellation_reason == cancellation_reason for v in deleted_user_jobs]
        )
        assert all([v.status == JobStatus.SUCCESSFUL for v in other_user_jobs])


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_delete_profile_cancels_executing_jobs(
    client, worker, job, jobs_folder, mocker: MockerFixture, fake_clock
):
    with client as client:
        users = create_many_users(client, USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])
        # preprocessed and executed, waiting for postprocessing
        worker.run(is_burst=True, max_jobs=2)
        assert get_stored_jobs()[0].status == JobStatus.EXECUTING

        delete_own_profile(client, user_id=job_info["user_id"])

        assert get_stored_jobs()[0].status == JobStatus.CANCELLED


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_delete_profile_during_booking_releases_the_waitlist(
    client, worker, job, jobs_folder, mocker: MockerFixture, fake_clock
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
        worker.run(is_burst=True)

        delete_own_profile(client, user_id=booker["id"])
        worker.run(is_burst=True)

        assert get_stored_jobs()[0].status == JobStatus.SUCCESSFUL


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_admin_remove_user(
    client, worker, job, jobs_folder, mocker: MockerFixture, fake_clock
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
        wait_on_worker(worker, clock=fake_clock)

        stored_jobs = get_stored_jobs()

        deleted_user_jobs = [job for job in stored_jobs if job.user_id == curr_user_id]
        other_user_jobs = [job for job in stored_jobs if job.user_id != curr_user_id]
        cancellation_reason = "Cancelled on user deletion"

        assert all([v.status == JobStatus.CANCELLED for v in deleted_user_jobs])
        assert all(
            [v.cancellation_reason == cancellation_reason for v in deleted_user_jobs]
        )
        assert all([v.status == JobStatus.SUCCESSFUL for v in other_user_jobs])


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS[:-1])
def test_non_admin_remove_user(
    client, worker, job, jobs_folder, mocker: MockerFixture, fake_clock
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
        wait_on_worker(worker, clock=fake_clock)

        stored_jobs = get_stored_jobs()
        assert all([v.status == JobStatus.SUCCESSFUL for v in stored_jobs])
