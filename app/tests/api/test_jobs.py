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
"""Tests for the API's job routes, and the jobs' way through the queues"""
import dataclasses
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from unittest.mock import Mock
from uuid import uuid4

import pytest
import time_machine
from pytest_mock import MockerFixture

from app.libs.queues.dtos import Job, JobStatus, Stage, Timestamps
from app.services.booking import Booking
from app.services.scheduler import dispatcher as scheduler_dispatcher
from app.services.scheduler.dispatcher import fail_overdue_jobs
from app.tests.utils.api import (
    create_mss_headers,
    get_bearer_headers,
    get_token,
)
from app.tests.utils.bookings import (
    create_booking,
)
from app.tests.utils.env import (
    TEST_JWT_TTL,
)
from app.tests.utils.jobs import (
    cancel_job_via_mss,
    claim_job_long_ago,
    get_job_submission_metadata,
    get_jobs_sent_to_mss,
    get_raw_jobs,
    get_stored_jobs,
    is_booking_excluded,
    is_job_result_matching,
    save_job_file,
    spy_on_mss_updates,
    submit_jobs,
    view_job,
)
from app.tests.utils.params import (
    ALL_INVALID_UPLOAD_JOB_PARAMS,
    ALL_UPLOAD_JOB_PARAMS,
    FASTAPI_CLIENTS,
    HAS_QUANTIFY,
    SIMPLE_UPLOAD_JOB_PARAMS,
    USERS,
    VIEW_JOB_PARAMS,
    VIEW_JOBS_PARAMS,
)
from app.tests.utils.records import drop_microseconds, order_by, paginate
from app.tests.utils.users import (
    create_many_users,
    create_user,
)
from app.tests.utils.workers import TestWorker, set_worker_executor, wait_on_worker

# (patch target, patch kwargs) making one stage of a job fail; "executor" is the worker's
_FAILING_STAGE_PATCHES = [
    (
        "app.services.scheduler.tasks.decompress_qobj",
        {"side_effect": ValueError("unreadable qobj")},
    ),
    (
        "executor",
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
    # the dispatcher stops a stage past its time limit by raising this inside it
    (
        "app.services.scheduler.tasks.decompress_qobj",
        {"side_effect": TimeoutError("preprocessing timed out")},
    ),
    (
        "executor",
        {
            "return_value": Mock(
                preprocess=Mock(return_value=(0.2, None)),
                run=Mock(side_effect=TimeoutError("execution timed out")),
            )
        },
    ),
    (
        "app.services.scheduler.tasks.read_job_from_hdf5",
        {"side_effect": TimeoutError("postprocessing timed out")},
    ),
]


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
def test_job_token_is_valid_on_a_device_whose_current_date_is_past(client, monkeypatch):
    """BCC_CURRENT_DATE stands in for now for bookings, but a token expires by the real clock"""
    import settings

    with client as client:
        device_settings = settings.get_device_settings(os.environ["BCC_DEFAULT_PREFIX"])
        monkeypatch.setattr(
            device_settings, "current_date", datetime(2025, 10, 1, tzinfo=timezone.utc)
        )
        job_id = f"{uuid4()}"
        token, _ = get_token(client, data={"user_id": f"{uuid4()}", "job_id": job_id})

        response = client.post(
            f"/jobs/{job_id}/cancel", json={}, headers=get_bearer_headers(token)
        )

        assert response.status_code == 404


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


@pytest.mark.simulated
@pytest.mark.parametrize(
    "client, worker, job, expected_counts, device",
    ALL_UPLOAD_JOB_PARAMS,
)
def test_submit_jobs_no_booking(
    client,
    worker,
    job,
    expected_counts,
    device,
    jobs_folder,
    mocker: MockerFixture,
    mss_conn_spy,
    sql_engine_spy,
    fake_clock,
):
    """POST '/jobs' when there is no booking should run the jobs in FIFO (first in, first out)"""
    with client as client:
        sql_engines_at_start = sql_engine_spy.call_count
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
        worker.run(is_burst=True)

        stored_jobs = get_stored_jobs()
        stored_jobs.sort(key=lambda v: v.timestamps.execution.start_timestamp)

        expected_stored_jobs = [
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
            )
            for job_id, job_in_db in zip_longest(
                job_ids_in_fifo, stored_jobs, fillvalue=None
            )
        ]

        # Assert that they are all complete and their timestamp of completion is in FIFO order
        assert stored_jobs == expected_stored_jobs

        user_count = len(users)
        for idx, job_in_db in enumerate(stored_jobs):
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

        # no connection per job: the app's and the worker's are made at their start-up,
        # MSS's lazily on the first send
        assert mss_conn_spy.call_count <= 1
        assert sql_engine_spy.call_count == sql_engines_at_start


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_in_active_booking(
    client,
    worker,
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
        wait_on_worker(worker, clock=fake_clock)

        stored_jobs = get_stored_jobs()
        stored_jobs.sort(key=lambda v: v.timestamps.execution.start_timestamp)

        booker_jobs = [job for job in stored_jobs if job.user_id == booker_id]
        non_booker_jobs = [job for job in stored_jobs if job.user_id != booker_id]
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

        assert all([v.status == JobStatus.SUCCESSFUL for v in stored_jobs])
        assert last_booker_job_start < booking.end_utc
        assert drop_microseconds(first_non_booker_job_start) >= drop_microseconds(
            booking_end_timestamp
        )


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_in_idle_booking(
    client,
    worker,
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
        wait_on_worker(worker, clock=fake_clock)

        stored_jobs = get_stored_jobs()

        stored_jobs.sort(key=lambda v: v.timestamps.execution.start_timestamp)
        booker_jobs = [job for job in stored_jobs if job.user_id == booker_id]
        non_booker_jobs = [job for job in stored_jobs if job.user_id != booker_id]
        last_booker_job = booker_jobs[-1]
        first_non_booker_job = non_booker_jobs[0]

        # Assert that they are all complete and their booker jobs started before booking end_utc
        # and also non booker jobs started before end_utc
        # Note: Enqueue_at ignores microseconds
        booking_end_timestamp = booking.end_utc
        last_booker_job_start = last_booker_job.start_utc
        first_non_booker_job_start = first_non_booker_job.start_utc

        assert all([v.status == JobStatus.SUCCESSFUL for v in stored_jobs])
        assert last_booker_job_start < booking_end_timestamp
        if last_booker_job.actual_duration < booking.total_duration:
            assert first_non_booker_job_start < booking_end_timestamp


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_in_idle_booking_before_another(
    client,
    worker,
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
        mss_update_spy = spy_on_mss_updates(mocker)
        wait_on_worker(worker, clock=fake_clock)

        jobs_in_db = get_stored_jobs()

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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_booker_job_longer_than_the_time_left_fails(
    client, worker, job, jobs_folder, mocker, fake_clock
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
        worker.run(is_burst=True, max_jobs=1)

        job_in_db = get_stored_jobs()[0]
        assert job_in_db.status == JobStatus.FAILED
        assert (
            job_in_db.failure_reason == "job too long for the time left in the booking"
        )


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_job_whose_booking_ended_in_preprocessing_runs(
    client, worker, job, jobs_folder, mocker, fake_clock
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
        worker.run(is_burst=True)

        assert get_stored_jobs()[0].status == JobStatus.SUCCESSFUL


@pytest.mark.skipif(
    not HAS_QUANTIFY,
    reason="the simulators compile for long enough to reorder the jobs before the booking",
)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_long_jobs_before_booking(
    client,
    worker,
    job,
    jobs_folder,
    mocker,
    fake_clock,
):
    """POST long jobs to '/jobs' before booking starts runs the shorter ones before it, and the
    booker's long one in it"""
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
        wait_on_worker(worker, clock=fake_clock)

        jobs_in_db = get_stored_jobs()
        jobs_in_db.sort(key=lambda v: v.start_utc)
        job_estimated_durations = [v.estimated_duration for v in jobs_in_db]

        assert all([job.status == JobStatus.SUCCESSFUL for job in jobs_in_db])
        # the small-enough first (0.3, 0.9, 0.2, 0.1), then, in the booking, the booker's
        # 2.9, then the others' bigger ones (3.0, 4.0) in original order
        assert job_estimated_durations == [0.3, 0.9, 0.2, 0.1, 2.9, 3.0, 4.0]


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_invalid_token(client, worker, job, jobs_folder):
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_submit_jobs_without_token(client, worker, job, jobs_folder):
    """POST to '/jobs' jobs without a token results in an error"""
    with client as client:
        job_file_path = save_job_file(folder=jobs_folder, job=job)
        with open(job_file_path, "rb") as file:
            response = client.post("/jobs", files={"upload_file": file})

        json_response = response.json()

        assert response.status_code == 401
        assert json_response["detail"] == "user not authenticated"


@pytest.mark.parametrize("client, worker, job", ALL_INVALID_UPLOAD_JOB_PARAMS)
def test_upload_invalid_job(client, worker, job, jobs_folder, mocker, fake_clock):
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

        worker.run(is_burst=True)
        jobs_in_db = get_stored_jobs()
        assert jobs_in_db == []


@pytest.mark.timeout(60)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_duplicate_job_upload(client, jobs_folder, worker, job, mocker, fake_clock):
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

            worker.run(is_burst=True)

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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_remove_job(client, jobs_folder, worker, job, mocker, fake_clock):
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
        worker.run(is_burst=True)

        # initiate delete
        job_info = job_metadata_list[0]
        headers = create_mss_headers(user_id=job_info["user_id"])
        deletion_response = client.delete(
            f"/jobs/{job_info["job_id"]}", headers=headers
        )
        # run the rest of the tasks
        worker.run(is_burst=True)

        jobs_in_db = get_stored_jobs()
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list[1:]])
        assert deletion_response.status_code == 200
        assert job_ids_in_db == expected_ids


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_remove_another_users_job(client, jobs_folder, worker, job, mocker, fake_clock):
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
        worker.run(is_burst=True)

        # initiate delete
        another_user = users[1]
        job_info = job_metadata_list[0]
        job_id = job_info["job_id"]
        headers = create_mss_headers(user_id=another_user["id"])
        deletion_response = client.delete(f"/jobs/{job_id}", headers=headers)
        # run the rest of the tasks
        worker.run(is_burst=True)

        jobs_in_db = get_stored_jobs()
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list])
        assert deletion_response.status_code == 404
        assert deletion_response.json() == {"detail": f"Job {job_id} not found"}
        assert job_ids_in_db == expected_ids


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_admin_remove_job(client, jobs_folder, worker, job, mocker, fake_clock):
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
        worker.run(is_burst=True)

        # initiate delete
        another_user = users[1]
        job_info = job_metadata_list[0]
        headers = create_mss_headers(user_id=another_user["id"], is_admin=True)
        deletion_response = client.delete(
            f"/jobs/{job_info["job_id"]}", headers=headers
        )
        # run the rest of the tasks
        worker.run(is_burst=True)

        jobs_in_db = get_stored_jobs()
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list[1:]])
        assert deletion_response.status_code == 200
        assert job_ids_in_db == expected_ids


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_remove_job(
    client, jobs_folder, worker, job, mocker, fake_clock
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
        worker.run(is_burst=True)

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
        worker.run(is_burst=True)

        jobs_in_db = get_stored_jobs()
        job_ids_in_db = sorted([v.job_id for v in jobs_in_db])
        expected_ids = sorted([v["job_id"] for v in job_metadata_list])
        assert job_ids_in_db == expected_ids


@pytest.mark.parametrize("client, worker, job, device_name, user", VIEW_JOB_PARAMS)
def test_view_job(
    client,
    worker,
    job,
    device_name,
    user,
    jobs_folder,
    mocker: MockerFixture,
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
        worker.run(is_burst=True)

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
        )
        assert complete_job == expected_completed_job


@pytest.mark.parametrize("client, worker, job, device_name, user", VIEW_JOB_PARAMS)
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


@pytest.mark.parametrize("client, worker, job, device_name, user", VIEW_JOB_PARAMS[:3])
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


@pytest.mark.parametrize("client, worker, job, device_name, user", VIEW_JOB_PARAMS[:3])
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


@pytest.mark.parametrize("client, worker, job, device", VIEW_JOBS_PARAMS)
def test_view_jobs(
    client, worker, job, device, jobs_folder, mocker: MockerFixture, fake_clock
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
            ).model_dump(mode="json", exclude_none=True)
            for idx, job_id in enumerate(job_ids)
            if idx % user_count == 0
        ]

        # ensure ordering is the same
        pending_jobs_resp["data"] = order_by(pending_jobs_resp["data"], field="job_id")
        expected_jobs = order_by(expected_jobs, field="job_id")

        assert pending_jobs_resp == paginate(expected_jobs)

        # Run the queue to run the job and see that it is updated
        worker.run(is_burst=True)

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
            }
            for idx, v in enumerate(expected_jobs)
        ]

        # ensure ordering is the same
        completed_jobs_resp["data"] = order_by(
            completed_jobs_resp["data"], field="job_id"
        )
        assert completed_jobs_resp == paginate(expected_jobs)


@pytest.mark.parametrize("client, worker, job, device", VIEW_JOBS_PARAMS)
def test_view_jobs_by_status(
    client, worker, job, device, jobs_folder, mocker: MockerFixture, fake_clock
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
        worker.run(is_burst=True)

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
            ).model_dump(mode="json", exclude_none=True)
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
            ).model_dump(mode="json", exclude_none=True)
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


@pytest.mark.parametrize("client, worker, job, device", VIEW_JOBS_PARAMS)
def test_unauthenticated_view_jobs(
    client, worker, job, device, jobs_folder, mocker: MockerFixture
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_job_via_mss(client, jobs_folder, worker, job, mocker, fake_clock):
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
        worker.run(is_burst=True, max_jobs=1)

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

        job_in_db = get_stored_jobs()[0]
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_job_directly(client, jobs_folder, worker, job, mocker, fake_clock):
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
        worker.run(is_burst=True, max_jobs=1)

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

        job_in_db = get_stored_jobs()[0]
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


@pytest.mark.parametrize("patch_target, patch_kwargs", _FAILING_STAGE_PATCHES)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_failed_job_is_marked_failed_and_sent_to_mss(
    client,
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
        if patch_target == "executor":
            set_worker_executor(worker, patch_kwargs["return_value"])
        else:
            mocker.patch(patch_target, **patch_kwargs)
        mss_update_spy = spy_on_mss_updates(mocker)
        with open(job_info["file_path"], "rb") as file:
            client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

        worker.run(is_burst=True)

        job_in_db = get_stored_jobs()[0]
        assert job_in_db.status == JobStatus.FAILED
        assert get_jobs_sent_to_mss(mss_update_spy)[-1].status == JobStatus.FAILED


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_finished_job_is_sent_to_mss_with_its_results(
    client, jobs_folder, worker, job, mocker, mock_mss, fake_clock
):
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])

        worker.run(is_burst=True)

        job_updates = [
            event["data"]
            for event in mock_mss.sent_events
            if event["name"] == "job_updated"
        ]
        assert {update["job_id"] for update in job_updates} == {job_info["job_id"]}
        assert job_updates[-1]["status"] == "successful"
        assert job_updates[-1]["result"]["memory"]


@pytest.mark.parametrize(
    "query, expected_is_excluded", [("?force_normal_queue=true", True), ("", False)]
)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_job_forced_to_the_normal_queue_waits_outside_bookings(
    client, jobs_folder, worker, job, mocker, query, expected_is_excluded
):
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                f"/jobs{query}",
                files={"upload_file": file},
                headers=job_info["headers"],
            )

        assert response.status_code == 200
        assert is_booking_excluded(job_info["job_id"]) == expected_is_excluded


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancelled_job_is_not_executed(
    client, jobs_folder, worker, job, mocker, fake_clock
):
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
        worker.run(is_burst=True, max_jobs=1)

        cancel_job_via_mss(client, user_id=job_info["user_id"], job_id=job_id)
        worker.run(is_burst=True)

        [stored_job] = get_stored_jobs()
        assert (stored_job.status, stored_job.stage) == (
            JobStatus.CANCELLED,
            Stage.EXEC_Q,
        )


@pytest.mark.parametrize(
    "max_execution_time, expected_status",
    [(600, JobStatus.FAILED), (0, JobStatus.EXECUTING)],
)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_job_whose_worker_died_fails_after_its_time_limit(
    client,
    jobs_folder,
    worker,
    job,
    mocker,
    fake_clock,
    max_execution_time,
    expected_status,
):
    """A time limit of 0 is none, so it never fails a job"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])
        worker.run(is_burst=True, max_jobs=1)
        device = next(iter(worker.device_resources.values()))
        claim_job_long_ago(device, job_id=job_info["job_id"], stage=Stage.EXEC_W)
        device = dataclasses.replace(
            device,
            settings=device.settings.model_copy(
                update={"max_execution_time": max_execution_time}
            ),
        )

        fail_overdue_jobs(device)

        [stored_job] = get_stored_jobs()
        assert stored_job.status == expected_status


@pytest.mark.timeout(60)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_waiting_workers_are_woken_for_a_new_job(
    client, jobs_folder, worker, job, mocker
):
    """Workers that would otherwise wait a minute run a new job through at once"""
    mocker.patch.object(scheduler_dispatcher, "_MAX_WAIT_SECONDS", 60)
    devices = worker.device_resources
    workers = [
        TestWorker(devices, should_run_heavy=True, should_run_light=False),
        TestWorker(devices, should_run_heavy=False, should_run_light=True),
    ]
    threads = [
        threading.Thread(target=waiting_worker.run) for waiting_worker in workers
    ]
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        for thread in threads:
            thread.start()
        try:
            submit_jobs(client, data=[job_info])
            stored_job = _wait_for_job_to_end(max_wait_seconds=30)
        finally:
            for waiting_worker in workers:
                waiting_worker.stop()
            for thread in threads:
                thread.join()

    assert stored_job.status == JobStatus.SUCCESSFUL


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_mss_admin_new_to_the_device_views_and_cancels_job(
    client, jobs_folder, worker, job, mocker
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_finished_job(client, jobs_folder, worker, job, mocker, fake_clock):
    """POST '/jobs/{id}/cancel' leaves a job that has finished as it is"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])
        worker.run(is_burst=True)

        response = cancel_job_via_mss(
            client, user_id=job_info["user_id"], job_id=job_info["job_id"]
        )

        assert response.status_code == 406
        assert get_stored_jobs()[0].status == JobStatus.SUCCESSFUL


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_remove_cancelled_job(client, jobs_folder, worker, job, mocker, fake_clock):
    """DELETE '/jobs/{id}' deletes a job that was cancelled"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        user_id, job_id = job_info["user_id"], job_info["job_id"]
        submit_jobs(client, data=[job_info])
        worker.run(is_burst=True, max_jobs=1)
        cancel_job_via_mss(client, user_id=user_id, job_id=job_id)

        response = client.delete(f"/jobs/{job_id}", headers=create_mss_headers(user_id))

        assert response.status_code == 200
        assert get_stored_jobs() == []


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_cancel_another_users_job(client, jobs_folder, worker, job, mocker, fake_clock):
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
        worker.run(is_burst=True, max_jobs=1)

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

        job_in_db = get_stored_jobs()[0]

        expected_job = original_job.model_copy(
            update={
                "stage": Stage.EXEC_Q,
                "estimated_duration": job_in_db.estimated_duration,
                "updated_at": job_in_db.updated_at,
                "timestamps": job_in_db.timestamps,
            }
        )

        assert job_in_db == expected_job


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_admin_cancel_job(client, jobs_folder, worker, job, mocker, fake_clock):
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
        worker.run(is_burst=True, max_jobs=1)

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

        job_in_db = get_stored_jobs()[0]
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_cancel_job(
    client, worker, job, jobs_folder, mocker: MockerFixture, fake_clock
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
        worker.run(is_burst=True, max_jobs=1)

        payload = {"reason": cancellation_reason}
        url = f"/jobs/{job_id}/cancel"

        response = client.post(url, json=payload)
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}


def _wait_for_job_to_end(max_wait_seconds: float) -> Job:
    """Waits for the test device's one job to end, returning it as it ended or as it stands"""
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        [stored_job] = get_stored_jobs()
        if stored_job.status.is_terminal():
            break
        time.sleep(0.1)
    return stored_job
