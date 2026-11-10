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
"""Utilities for the jobs the tests submit through the API"""

import copy
import json
import os
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    TypedDict,
)
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
from httpx import Response
from pydantic import ValidationError
from pytest_mock import MockerFixture
from sqlalchemy import create_engine
from sqlmodel import Session

import services.scheduler.dispatcher as scheduler_dispatcher
import services.scheduler.tasks as scheduler_tasks
from libs.queues.dtos import Job, Stage
from services.scheduler import DeviceResources, JobsStore
from services.scheduler.store import JobRow
from services.scheduler.utils import update_job_in_mss, update_job_stage
from tests.utils.api import (
    create_mss_headers,
    get_bearer_headers,
    get_token,
)
from tests.utils.env import TEST_BOOKING_DB_URL
from tests.utils.params import USERS
from tests.utils.users import create_many_users
from utils.datetime import get_utc_now


def cancel_job_via_mss(
    client: "TestClient",
    user_id: str,
    job_id: str,
    reason: Optional[str] = None,
    is_admin: Optional[bool] = None,
) -> "Response":
    """Cancels the job on the REST API via MSS, passing signed MSS headers

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


def view_job(
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


def get_raw_jobs(job_template: dict, durations: List[float]) -> List[dict]:
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

        with suppress(TypeError, KeyError, IndexError):
            set_test_duration(new_job, duration)

        raw_jobs.append(new_job)
    return raw_jobs


def get_test_duration(job: Dict[str, Any]) -> float:
    """Gets the seconds the job takes in the tests, from its first circuit's metadata"""
    return job["params"]["circuits"][0]["metadata"]["test_duration"]


def set_test_duration(job: Dict[str, Any], duration: float) -> None:
    """Makes the job take the seconds in the tests, in its first circuit's metadata"""
    job["params"]["circuits"][0]["metadata"]["test_duration"] = duration


def get_stored_jobs() -> List[Job]:
    """Gets the jobs the test device has stored, least recently written first"""
    # an engine of the test's own, not the app's, which tests count
    store = JobsStore(
        create_engine(TEST_BOOKING_DB_URL),
        device_name=os.environ["BCC_DEFAULT_PREFIX"],
        # only read, so never expiring any
        ttl=0,
    )
    return store.find()


def claim_job_long_ago(device: DeviceResources, job_id: str, stage: Stage) -> None:
    """Leaves the job at the stage, claimed a day ago by a worker that has since died"""
    update_job_stage(device.jobs_store, job_id=job_id, stage=stage)
    with Session(device.bookings_db) as session:
        row = session.get(JobRow, (device.settings.default_prefix, job_id))
        row.claimed_at = get_utc_now() - timedelta(days=1)
        session.add(row)
        session.commit()


def is_booking_excluded(job_id: str) -> bool:
    """Whether the test device's job waits for the time outside bookings"""
    with Session(create_engine(TEST_BOOKING_DB_URL)) as session:
        row = session.get(JobRow, (os.environ["BCC_DEFAULT_PREFIX"], job_id))
    return row.is_booking_excluded


def spy_on_mss_updates(mocker: MockerFixture) -> Mock:
    """Spies, with one mock, on the jobs the tasks and the dispatcher send MSS"""
    spy = Mock(side_effect=update_job_in_mss)
    for module in (scheduler_tasks, scheduler_dispatcher):
        mocker.patch.object(module, "update_job_in_mss", spy)
    return spy


def get_jobs_sent_to_mss(mss_update_spy: Mock) -> List[Job]:
    """Gets the jobs, in order, that a spy on update_job_in_mss saw sent to MSS"""
    return [call.kwargs["payload"] for call in mss_update_spy.call_args_list]


def submit_one_job(
    client: "TestClient",
    job: Dict[str, Any],
    mocker: MockerFixture,
    jobs_folder: Path,
    duration: float = 0.2,
) -> "JobSubmissionMetadata":
    """Submits the job, taking the given seconds, as the first test user, whom it creates

    Args:
        client: the test client, entered
        job: the job, as uploaded
        mocker: the mocker for intercepting the generation of the user token
        jobs_folder: the folder to save the job file in before it is uploaded
        duration: the seconds the job takes in the tests

    Returns:
        the job's submission metadata
    """
    users = create_many_users(client, raw_users=USERS[:1])
    [raw_job] = get_raw_jobs(job, durations=[duration])
    [job_info] = get_job_submission_metadata(
        client, jobs=[raw_job], users=users, mocker=mocker, jobs_folder=jobs_folder
    )
    submit_jobs(client, data=[job_info])
    return job_info


def submit_jobs(
    client: "TestClient", data: List["JobSubmissionMetadata"] = None
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


def get_job_submission_metadata(
    client: "TestClient",
    jobs: List[dict],
    users: List[dict],
    mocker: MockerFixture,
    jobs_folder: Path,
    is_pulse_allowed: bool = False,
) -> List["JobSubmissionMetadata"]:
    """Generates a list of job submission metadata to use when submitting jobs

    Metadata includes the token to use, the user_id, the file path
    This operation is quite expensive

    Args:
        client: the test client to run the tests on
        jobs: list of jobs for which the map is constructed
        users: the list of users to use to submit the jobs in a round-robin style
        mocker: the mocker for intercepting the generation of the user token
        jobs_folder: the folder in which the job files are to be saved temporarily before submission
        is_pulse_allowed: whether MSS lets the jobs be pulse-level, as for their project

    Returns:
        a list of job submission metadata
    """
    num_of_users = len(users)
    results = []
    for idx, job_info in enumerate(jobs):
        curr_user = users[idx % num_of_users]
        job_file_path = save_job_file(folder=jobs_folder, job=job_info)
        user_id = curr_user["id"]
        job_id = job_info["job_id"]

        token, _ = get_token(
            client,
            data={
                "user_id": user_id,
                "job_id": job_id,
                "is_pulse_allowed": is_pulse_allowed,
            },
        )

        results.append(
            {
                "job_id": job_id,
                "user_id": user_id,
                "file_path": job_file_path,
                "token": token,
                "headers": get_bearer_headers(token),
            }
        )

    return results


def save_job_file(folder: Path, job: Dict[str, Any], ext: str = ".json") -> Path:
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


def is_job_result_matching(
    results: List[str], expected_min_counts: Dict[str, int]
) -> bool:
    """Matches job results, keeping in mind that the results are probabilistic

    The states are written in hex '0x0', '0x1', '0x2', '0x3' i.e. 00, 01, 10, 11

    Args:
        results: the results obtained
        expected_min_counts: the expected counts minimum counts for each state

    Returns:
        True if they match, False if they don't
    """
    return all([results.count(k) >= v for k, v in expected_min_counts.items()])


class JobSubmissionMetadata(TypedDict):
    job_id: str
    token: str
    user_id: str
    file_path: Path
    headers: dict
