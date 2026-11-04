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
"""Tests for the API's logfile routes"""

import json

import pytest

from tests.utils.api import (
    get_bearer_headers,
    get_token,
)
from tests.utils.jobs import (
    get_job_submission_metadata,
    save_job_file,
    submit_jobs,
)
from tests.utils.params import (
    SIMPLE_UPLOAD_JOB_PARAMS,
    USERS,
)
from tests.utils.users import (
    create_many_users,
    create_user,
)


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_download_logfile(
    logfile_download_folder, client, worker, job, mocker, jobs_folder
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

        response = client.get(f"/logfiles/{job['job_id']}", headers=job_info["headers"])
        file_content = json.loads(response.content)
        assert response.status_code == 200
        assert file_content == job


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_logfile_for_non_existing_job(
    logfile_download_folder, client, worker, job, mocker
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


@pytest.mark.parametrize("client, worker, job", SIMPLE_UPLOAD_JOB_PARAMS)
def test_unauthenticated_download_logfile(
    logfile_download_folder, client, worker, job, mocker, jobs_folder
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
        response = client.get(f"/logfiles/{job['job_id']}")
        assert response.status_code == 401
        assert response.json() == {"detail": "user not authenticated"}

        # wrong authentication
        headers = get_bearer_headers("foo")
        response = client.get(f"/logfiles/{job['job_id']}", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "not authenticated"}
