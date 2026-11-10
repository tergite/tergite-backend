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
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
import time_machine
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture

from api.routers.jobs import JOBS_PAGE_SIZE
from libs.queues.dtos import Job, JobStatus, Stage, Timestamps
from services.booking import Booking
from services.scheduler import DeviceResources, WorkerDeadline
from services.scheduler import dispatcher as scheduler_dispatcher
from services.scheduler import service as scheduler_service
from services.scheduler import tasks as scheduler_tasks
from services.scheduler.dispatcher import (
    fail_jobs_queued_too_long,
    fail_overdue_jobs,
)
from services.scheduler.utils import mark_job_failed
from tests.utils.api import (
    create_mss_headers,
    get_bearer_headers,
    get_token,
)
from tests.utils.bookings import (
    create_booking,
)
from tests.utils.env import (
    TEST_JWT_TTL,
    TEST_MAX_IDLE_TIME,
)
from tests.utils.jobs import (
    JobSubmissionMetadata,
    cancel_job_via_mss,
    claim_job_long_ago,
    get_job_submission_metadata,
    get_jobs_sent_to_mss,
    get_raw_jobs,
    get_stored_jobs,
    get_test_duration,
    is_booking_excluded,
    is_job_result_matching,
    save_job_file,
    set_test_duration,
    spy_on_mss_updates,
    submit_jobs,
    submit_one_job,
    view_job,
)
from tests.utils.params import (
    ALL_INVALID_UPLOAD_JOB_PARAMS,
    ALL_UPLOAD_JOB_PARAMS,
    FASTAPI_CLIENTS,
    QUANTIFY_UPLOAD_JOB_PARAMS,
    SIMPLE_UPLOAD_JOB_PARAMS,
    USERS,
    VIEW_JOB_PARAMS,
    VIEW_JOBS_PARAMS,
)
from tests.utils.records import drop_microseconds, order_by, paginate
from tests.utils.users import (
    create_many_users,
    create_user,
)
from tests.utils.workers import TestWorker, set_worker_executor, wait_on_worker

# (patch target, patch kwargs) making one stage of a job fail; "executor" is the worker's
_FAILING_STAGE_PATCHES = [
    (
        "services.scheduler.tasks.preprocess_circuits",
        {"side_effect": ValueError("uncompilable circuits")},
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
        "services.scheduler.tasks.read_job_from_hdf5",
        {"side_effect": OSError("unreadable results")},
    ),
    # the dispatcher stops a stage past its time limit by raising this inside it
    (
        "services.scheduler.tasks.preprocess_circuits",
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
        "services.scheduler.tasks.read_job_from_hdf5",
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


# the 2-qubit simulation alone takes about a minute on a fast machine, beside other workers more
@pytest.mark.timeout(600)
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

        # the last the shortest, which a queue that ran the shortest first would run first;
        # no more, as each is a simulation that takes seconds on the 2-qubit simulator
        durations = [0.4, 3, 0.1]
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

            # on the fake clock, a stage that takes no simulated time ends as it starts
            assert pre_processing.start_timestamp <= pre_processing.finish_timestamp
            assert pre_processing.finish_timestamp <= execution.start_timestamp
            assert execution.start_timestamp <= execution.finish_timestamp
            assert execution.finish_timestamp <= post_processing.start_timestamp
            assert post_processing.start_timestamp <= post_processing.finish_timestamp
            assert post_processing.finish_timestamp <= final.start_timestamp
            assert final.start_timestamp <= final.finish_timestamp

        # no connection per job: the app's and the worker's are made at their start-up,
        # MSS's lazily on the first send
        assert mss_conn_spy.call_count <= 1
        assert sql_engine_spy.call_count == sql_engines_at_start


@pytest.mark.real_time
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


@pytest.mark.real_time
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


@pytest.mark.real_time
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
            if get_test_duration(v) in non_booker_pre_2dn_slot_durations
        ]
        expected_non_booker_job_ids_post_2nd_slot = [
            v["job_id"]
            for v in raw_jobs
            if get_test_duration(v)
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


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_others_jobs_run_once_a_booker_whose_execution_failed_is_idle(
    client, worker, job, jobs_folder, mocker, fake_clock
):
    """A job failed or cancelled in execution is left at that stage"""
    with client as client:
        [booker, other_user] = create_many_users(client, USERS[:2])
        create_booking(
            client, user_id=booker["id"], booking={"duration": 60, "starts_in": 0}
        )
        [booker_job, other_job] = get_job_submission_metadata(
            client,
            jobs=get_raw_jobs(job, durations=[0.2, 0.2]),
            users=[booker, other_user],
            mocker=mocker,
            jobs_folder=jobs_folder,
        )
        submit_jobs(client, data=[booker_job])
        failing_execution = mocker.patch.object(
            scheduler_tasks, "run_job", side_effect=RuntimeError("device lost")
        )
        worker.run(is_burst=True)
        mocker.stop(failing_execution)
        submit_jobs(client, data=[other_job])

        fake_clock.shift(TEST_MAX_IDLE_TIME + 1)
        worker.run(is_burst=True)

        stored_jobs = {job.job_id: job.status for job in get_stored_jobs()}
        assert stored_jobs == {
            booker_job["job_id"]: JobStatus.FAILED,
            other_job["job_id"]: JobStatus.SUCCESSFUL,
        }


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


@pytest.mark.real_time
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

    expected_err_resp = {"detail": f"job {job_info['job_id']} already exists"}
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
            f"/jobs/{job_info['job_id']}", headers=headers
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
            f"/jobs/{job_info['job_id']}", headers=headers
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
            client, user={**user, "email": f"extra-{user['email']}"}
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
            client, user={**user, "email": f"extra-{user['email']}"}
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
            client, user={**user, "email": f"extra-{user['email']}"}
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


@pytest.mark.parametrize("query", ["?limit=0", "?limit=1001", "?skip=-1"])
@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_jobs_are_listed_only_in_pages_of_at_most_a_thousand(client, query):
    with client as client:
        response = client.get(f"/jobs{query}", headers=create_mss_headers())

    assert response.status_code == 422


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_jobs_are_listed_a_hundred_at_a_time_unless_asked(client):
    """So that a user's backlog is not read and sent whole"""
    with client as client:
        response = client.get("/jobs", headers=create_mss_headers())

    assert (response.status_code, response.json()["limit"]) == (200, 100)


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
            set_test_duration(new_job, duration)
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

        assert pending_jobs_resp == paginate(expected_jobs, limit=JOBS_PAGE_SIZE)

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
        assert completed_jobs_resp == paginate(expected_jobs, limit=JOBS_PAGE_SIZE)


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
            set_test_duration(new_job, duration)
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

        assert pending_jobs_resp == paginate(
            expected_pending_jobs, limit=JOBS_PAGE_SIZE
        )

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
        assert completed_jobs_resp == paginate(
            expected_completed_jobs, limit=JOBS_PAGE_SIZE
        )

        ## Get all jobs

        # refresh the headers
        headers = create_mss_headers(user_id)
        response = client.get("/jobs", headers=headers)
        all_jobs_resp = response.json()

        expected_all_jobs = expected_completed_jobs + expected_pending_jobs
        expected_all_jobs = order_by(expected_all_jobs, field="job_id")

        # ensure ordering is the same
        all_jobs_resp["data"] = order_by(all_jobs_resp["data"], field="job_id")
        assert all_jobs_resp == paginate(expected_all_jobs, limit=JOBS_PAGE_SIZE)


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
            set_test_duration(new_job, duration)
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_job_cancelled_directly_is_sent_to_mss(
    client, jobs_folder, worker, job, mocker, mock_mss, fake_clock
):
    """A job cancelled with its job token, not through MSS, still reaches MSS as cancelled"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])

        client.post(
            f"/jobs/{job_info['job_id']}/cancel",
            json={"reason": "just testing"},
            headers=job_info["headers"],
        )

        job_updates = [
            event["data"]
            for event in mock_mss.sent_events
            if event["name"] == "job_updated"
        ]
        assert job_updates[-1]["job_id"] == job_info["job_id"]
        assert job_updates[-1]["status"] == "cancelled"
        assert job_updates[-1]["cancellation_reason"] == "just testing"


@pytest.mark.parametrize("route", ["cancel", "delete"])
@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS[:1])
def test_api_answers_its_health_check_while_a_cancel_waits_on_mss(
    client, jobs_folder, worker, job, mocker, route
):
    """bcc start kills an API that misses its health checks, and MSS may take minutes"""
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        is_sending, is_sent = threading.Event(), threading.Event()

        def wait_on_mss(*args, **kwargs):
            is_sending.set()
            is_sent.wait(timeout=10)

        mocker.patch.object(
            scheduler_service, "update_job_in_mss", side_effect=wait_on_mss
        )
        if route == "cancel":
            cancel = threading.Thread(
                target=cancel_job_via_mss,
                args=(client, job_info["user_id"], job_info["job_id"]),
            )
        else:
            cancel = threading.Thread(
                target=client.delete,
                args=(f"/jobs/{job_info['job_id']}",),
                kwargs={"headers": create_mss_headers(user_id=job_info["user_id"])},
            )
        cancel.start()
        is_sending.wait(timeout=5)

        health_check = threading.Thread(target=client.get, args=("/health",))
        health_check.start()
        health_check.join(timeout=2)
        is_answered = not health_check.is_alive()
        is_sent.set()
        cancel.join()
        health_check.join()

        assert is_answered


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_worker_deadline_covers_the_whole_time_limit_of_its_stage(
    client, jobs_folder, worker, job, mocker
):
    """So that the process that started the worker takes it to be stuck only past it"""
    deadline = WorkerDeadline()
    worker_with_deadline = TestWorker(
        worker.device_resources,
        should_run_heavy=True,
        should_run_light=True,
        deadline=deadline,
    )
    seconds_left_in_execution = []
    execute = scheduler_tasks.execute

    def record_seconds_left(*args, **kwargs):
        seconds_left_in_execution.append(deadline.get_seconds_left())
        return execute(*args, **kwargs)

    mocker.patch.object(scheduler_tasks, "execute", side_effect=record_seconds_left)
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])

        worker_with_deadline.run(is_burst=True)

    device = next(iter(worker.device_resources.values()))
    assert seconds_left_in_execution[0] > device.settings.max_execution_time


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_job_finished_while_mss_is_unreachable_reaches_it_once_back(
    client, jobs_folder, worker, job, mocker, mock_mss, fake_clock
):
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])

        mock_mss.is_unreachable = True
        worker.run(is_burst=True)
        assert get_stored_jobs()[0].status == JobStatus.SUCCESSFUL

        mock_mss.is_unreachable = False
        worker.run(is_burst=True)
        job_updates = [
            event["data"]
            for event in mock_mss.sent_events
            if event["name"] == "job_updated"
        ]
        assert job_updates[-1]["status"] == "successful"
        assert job_updates[-1]["result"]["memory"]


@pytest.mark.parametrize("patch_target, patch_kwargs", _FAILING_STAGE_PATCHES)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_job_failed_while_mss_is_unreachable_reaches_it_once_back(
    client,
    jobs_folder,
    worker,
    job,
    mocker,
    mock_mss,
    fake_clock,
    patch_target,
    patch_kwargs,
):
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
        submit_jobs(client, data=[job_info])

        mock_mss.is_unreachable = True
        worker.run(is_burst=True)
        assert get_stored_jobs()[0].status == JobStatus.FAILED

        mock_mss.is_unreachable = False
        worker.run(is_burst=True)
        job_updates = [
            event["data"]
            for event in mock_mss.sent_events
            if event["name"] == "job_updated"
        ]
        assert job_updates[-1]["status"] == "failed"


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_dead_workers_job_failed_while_mss_is_unreachable_reaches_it_once_back(
    client, jobs_folder, worker, job, mocker, mock_mss, fake_clock
):
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

        mock_mss.is_unreachable = True
        worker.run(is_burst=True)
        assert get_stored_jobs()[0].status == JobStatus.FAILED

        mock_mss.is_unreachable = False
        worker.run(is_burst=True)
        job_updates = [
            event["data"]
            for event in mock_mss.sent_events
            if event["name"] == "job_updated"
        ]
        assert job_updates[-1]["status"] == "failed"


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


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_job_cancelled_while_it_postprocesses_stays_cancelled(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        read_results = scheduler_tasks.read_job_from_hdf5

        def cancel_then_read_results(*args, **kwargs):
            cancel_job_via_mss(
                client, user_id=job_info["user_id"], job_id=job_info["job_id"]
            )
            return read_results(*args, **kwargs)

        mocker.patch.object(
            scheduler_tasks, "read_job_from_hdf5", side_effect=cancel_then_read_results
        )
        mss_update_spy = spy_on_mss_updates(mocker)

        worker.run(is_burst=True)

        [stored_job] = get_stored_jobs()
        sent_statuses = [job.status for job in get_jobs_sent_to_mss(mss_update_spy)]
        assert stored_job.status == JobStatus.CANCELLED
        assert JobStatus.SUCCESSFUL not in sent_statuses


@pytest.mark.parametrize(
    "error", [RuntimeError("device lost"), TimeoutError("execution timed out")]
)
@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_job_cancelled_while_it_executes_stays_cancelled_if_the_execution_fails(
    client, jobs_folder, worker, job, mocker, fake_clock, error
):
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)

        def cancel_then_fail(*args, **kwargs):
            cancel_job_via_mss(
                client, user_id=job_info["user_id"], job_id=job_info["job_id"]
            )
            raise error

        mocker.patch.object(scheduler_tasks, "run_job", side_effect=cancel_then_fail)

        worker.run(is_burst=True)

        [stored_job] = get_stored_jobs()
        assert stored_job.status == JobStatus.CANCELLED


@pytest.mark.parametrize("task_name", ["preprocess", "execute", "postprocess"])
@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_job_deleted_as_a_stage_ends_leaves_the_worker_running(
    client, jobs_folder, worker, job, mocker, fake_clock, task_name
):
    """A worker that stops on it stops bcc start, and so the whole backend"""
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        task = getattr(scheduler_tasks, task_name)

        def run_then_delete(*args, **kwargs):
            result = task(*args, **kwargs)
            client.delete(
                f"/jobs/{job_info['job_id']}",
                headers=create_mss_headers(user_id=job_info["user_id"]),
            )
            return result

        mocker.patch.object(scheduler_tasks, task_name, side_effect=run_then_delete)

        worker.run(is_burst=True)

        assert get_stored_jobs() == []


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_job_deleted_while_it_postprocesses_leaves_the_worker_running(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        read_results = scheduler_tasks.read_job_from_hdf5

        def delete_then_read_results(*args, **kwargs):
            client.delete(
                f"/jobs/{job_info['job_id']}",
                headers=create_mss_headers(user_id=job_info["user_id"]),
            )
            return read_results(*args, **kwargs)

        mocker.patch.object(
            scheduler_tasks, "read_job_from_hdf5", side_effect=delete_then_read_results
        )

        worker.run(is_burst=True)

        assert get_stored_jobs() == []


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_job_deleted_before_a_sweep_fails_it_leaves_the_worker_running(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    """The sweep of overdue jobs picks them first and fails them after"""
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        worker.run(is_burst=True, max_jobs=1)
        device = next(iter(worker.device_resources.values()))
        claim_job_long_ago(device, job_id=job_info["job_id"], stage=Stage.EXEC_W)
        mark_failed = scheduler_dispatcher.mark_job_failed

        def delete_then_mark_failed(*args, **kwargs):
            device.jobs_store.delete(job_info["job_id"])
            return mark_failed(*args, **kwargs)

        mocker.patch.object(
            scheduler_dispatcher, "mark_job_failed", side_effect=delete_then_mark_failed
        )

        fail_overdue_jobs(device)

        assert get_stored_jobs() == []


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_job_timed_out_in_execution_is_failed_once(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    with client as client:
        submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        mocker.patch.object(
            scheduler_tasks, "run_job", side_effect=TimeoutError("took too long")
        )
        mss_update_spy = spy_on_mss_updates(mocker)

        worker.run(is_burst=True)

        failures_sent = [
            job.failure_reason
            for job in get_jobs_sent_to_mss(mss_update_spy)
            if job.status == JobStatus.FAILED
        ]
        assert failures_sent == ["timed out"]


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_job_that_ended_is_not_failed_by_a_sweep_that_picked_it_before(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    """The sweeps of overdue and long-queued jobs pick them first and fail them after, so
    one may end in between"""
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        worker.run(is_burst=True)
        device = next(iter(worker.device_resources.values()))

        mark_job_failed(
            device.jobs_store,
            job_id=job_info["job_id"],
            reason="timed out",
            job_supervisor_log=device.settings.job_supervisor_log,
        )

        [stored_job] = get_stored_jobs()
        assert (stored_job.status, stored_job.failure_reason) == (
            JobStatus.SUCCESSFUL,
            None,
        )


@pytest.mark.parametrize(
    "max_execution_queue_time, expected_status, expected_reason",
    [
        (60, JobStatus.FAILED, "waited longer than 60 s to execute"),
        (0, JobStatus.PENDING, None),
    ],
)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_job_waiting_to_execute_past_the_limit_fails(
    client,
    jobs_folder,
    worker,
    job,
    mocker,
    fake_clock,
    max_execution_queue_time,
    expected_status,
    expected_reason,
):
    """A job that finds no room to execute, e.g. between bookings, is failed rather than
    left to wait; a limit of 0 is none"""
    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        raw_jobs = get_raw_jobs(job, durations=[0.2])
        job_info = get_job_submission_metadata(
            client, jobs=raw_jobs, users=users, mocker=mocker, jobs_folder=jobs_folder
        )[0]
        submit_jobs(client, data=[job_info])
        worker.run(is_burst=True, max_jobs=1)
        device = next(iter(worker.device_resources.values()))
        device = dataclasses.replace(
            device,
            settings=device.settings.model_copy(
                update={"max_execution_queue_time": max_execution_queue_time}
            ),
        )
        fake_clock.shift(61)

        fail_jobs_queued_too_long(device)

        [stored_job] = get_stored_jobs()
        assert (stored_job.status, stored_job.failure_reason) == (
            expected_status,
            expected_reason,
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


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS[:1])
def test_files_of_a_deleted_job_are_deleted(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        worker.run(is_burst=True)
        device = next(iter(worker.device_resources.values()))
        job_files = _get_job_files(device, job_info["job_id"])
        is_each_kept_before = [file.exists() for file in job_files]

        client.delete(
            f"/jobs/{job_info['job_id']}",
            headers=create_mss_headers(user_id=job_info["user_id"]),
        )

        assert is_each_kept_before == [True, True]
        assert [file.exists() for file in job_files] == [False, False]


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS[:1])
def test_files_of_an_expired_job_are_deleted(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        worker.run(is_burst=True)
        device = next(iter(worker.device_resources.values()))
        job_files = _get_job_files(device, job_info["job_id"])

        fake_clock.shift(device.settings.jobs_store_ttl + 1)
        _submit_another_job(client, job, job_info, mocker, jobs_folder)
        worker.run(is_burst=True)

        assert [file.exists() for file in job_files] == [False, False]


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS[:1])
def test_job_at_a_stage_without_a_time_limit_outlives_the_store_ttl(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    """A time limit of 0 is none, so the job may stay at its stage unwritten"""
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        worker.run(is_burst=True, max_jobs=1)
        [device_name] = worker.device_resources
        device = worker.device_resources[device_name]
        claim_job_long_ago(device, job_id=job_info["job_id"], stage=Stage.EXEC_W)
        worker.device_resources[device_name] = dataclasses.replace(
            device,
            settings=device.settings.model_copy(update={"max_execution_time": 0}),
        )

        fake_clock.shift(device.settings.jobs_store_ttl + 1)
        _submit_another_job(client, job, job_info, mocker, jobs_folder)
        worker.run(is_burst=True)

        assert job_info["job_id"] in [job.job_id for job in get_stored_jobs()]


@pytest.mark.timeout(60)
@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_waiting_workers_are_woken_for_a_new_job(
    client, jobs_folder, worker, job, mocker
):
    """Workers that would otherwise wait a minute run a new job through at once"""
    devices = {
        name: dataclasses.replace(
            device,
            settings=device.settings.model_copy(update={"max_worker_wait_time": 60}),
        )
        for name, device in worker.device_resources.items()
    }
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


@pytest.mark.parametrize("client, worker, job", QUANTIFY_UPLOAD_JOB_PARAMS)
def test_cancel_of_a_job_that_ends_meanwhile_is_refused(
    client, jobs_folder, worker, job, mocker, fake_clock
):
    """A cancel reads the job before it writes it, so the job may end in between"""
    with client as client:
        job_info = submit_one_job(client, job, mocker=mocker, jobs_folder=jobs_folder)
        [job_before_it_ended] = get_stored_jobs()
        worker.run(is_burst=True)
        mocker.patch.object(
            scheduler_service, "get_job", return_value=job_before_it_ended
        )

        response = cancel_job_via_mss(
            client, user_id=job_info["user_id"], job_id=job_info["job_id"]
        )

        [stored_job] = get_stored_jobs()
        assert response.status_code == 406
        assert stored_job.status == JobStatus.SUCCESSFUL


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


def _get_job_files(device: DeviceResources, job_id: str) -> list[Path]:
    """Gets the job's uploaded file and its results file"""
    return [
        device.settings.job_upload_pool / job_id,
        device.settings.log_file_pool / f"{job_id}.hdf5",
    ]


def _submit_another_job(
    client: TestClient,
    job: dict,
    job_info: JobSubmissionMetadata,
    mocker: MockerFixture,
    jobs_folder: Path,
) -> None:
    """Submits a new job like the one of the info, for its user"""
    [other_job_info] = get_job_submission_metadata(
        client,
        jobs=get_raw_jobs(job, durations=[0.2]),
        users=[{"id": job_info["user_id"]}],
        mocker=mocker,
        jobs_folder=jobs_folder,
    )
    submit_jobs(client, data=[other_job_info])
