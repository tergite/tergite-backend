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
"""Tests for the API's switch routes"""
import os
import threading
from datetime import datetime

import pytest

from services.external.mss.service import MssClient
from tests.utils.api import (
    create_mss_headers,
)
from tests.utils.jobs import (
    get_job_submission_metadata,
    get_raw_jobs,
    get_stored_jobs,
)
from tests.utils.params import (
    FASTAPI_CLIENTS,
    SIMPLE_UPLOAD_JOB_PARAMS,
    USERS,
)
from tests.utils.users import (
    create_many_users,
)


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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_switch_off(client, jobs_folder, worker, job, mocker, fake_clock):
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

        worker.run(is_burst=True)

        stored_jobs = get_stored_jobs()
        assert stored_jobs == []


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_switch_on(client, jobs_folder, worker, job, mocker, fake_clock):
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

        worker.run(is_burst=True)

        stored_jobs = get_stored_jobs()
        assert len(stored_jobs) == 1


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_switch_answers_502_when_mss_refuses_the_event(client, mock_mss):
    with client as client:
        mock_mss.rejected_event_names = {"switched_off"}
        response = client.post("/switch/off", headers=create_mss_headers(is_admin=True))

        assert response.status_code == 502


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_switch_answers_504_when_mss_does_not_answer(client, mock_mss):
    with client as client:
        mock_mss.is_silent = True
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

    with client as client:
        # once started, as the start-up tells MSS too
        mocker.patch.object(MssClient, "send_event", side_effect=send_event_slowly)
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_switch_status(client, jobs_folder, worker, job, mocker):
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
