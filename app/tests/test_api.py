import copy
import json
import os
from itertools import zip_longest
from os import path
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.libs.queues.dtos import Job
from app.tests.conftest import (
    CLIENT_AND_RQ_WORKER_TUPLES,
    CLIENTS,
    FASTAPI_CLIENTS,
    MOCK_NOW,
)
from app.tests.utils.api import create_invalid_mss_headers, create_mss_headers
from app.tests.utils.fixtures import load_fixture
from app.tests.utils.http import get_headers
from app.tests.utils.records import (
    with_current_timestamps,
    with_incremental_timestamps,
)

_PARENT_FOLDER = path.dirname(path.abspath(__file__))
_JOBS_LIST = load_fixture("job_list.json")
_STATIC_PROPERTIES = [
    load_fixture("static_properties.json"),
    load_fixture("static_properties.simq1.json"),
    load_fixture("static_properties.simq2.json"),
]
_DYNAMIC_PROPERTIES = [
    load_fixture("dynamic_properties.json"),
    load_fixture("dynamic_properties.simq1.json"),
    load_fixture("dynamic_properties.simq2.json"),
]
_SIMULATOR_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload_simulator_1q.json")
_SIMULATOR_JOBS_FOR_UPLOAD_2Q = load_fixture("jobs_to_upload_simulator_2q.json")
_JOBS_FOR_UPLOAD = load_fixture("jobs_to_upload.json")
_INVALID_JOBS_FOR_UPLOAD = load_fixture("invalid_jobs_to_upload.json")
_JOB_ID_FIELD = "job_id"
_JOB_IDS = [item[_JOB_ID_FIELD] for item in _JOBS_LIST]
_JOBS_HASH_NAME = f"{Job.__module__}.{Job.__qualname__}".lower()
_DUMMY_JSON = {
    "foo": "bar",
    "os": "system",
}

_REGISTRATION_STAGE = "registration"
_PRE_PROCESSING_STAGE = "pre_processing"
_EXECUTION_STAGE = "execution"
_POST_PROCESSING_STAGE = "post_processing"
_FINAL_STAGE = "final"
_WRONG_APP_TOKENS = ["foohsjaghds", "barrr", "yeahhhjhdjf"]

# params
_UPLOAD_JOB_PARAMS = [
    (*CLIENT_AND_RQ_WORKER_TUPLES[0], job) for job in _JOBS_FOR_UPLOAD
]

_SIMULATOR_UPLOAD_JOB_PARAMS = [
    (*CLIENT_AND_RQ_WORKER_TUPLES[1], job) for job in _SIMULATOR_JOBS_FOR_UPLOAD
]

_SIMULATOR_UPLOAD_JOB_PARAMS_2Q = [
    (*CLIENT_AND_RQ_WORKER_TUPLES[2], job) for job in _SIMULATOR_JOBS_FOR_UPLOAD_2Q
]


_ALL_UPLOAD_JOB_PARAMS = (
    [(*args, {"0x0": 750}) for args in _UPLOAD_JOB_PARAMS]
    + [(*args, {"0x0": 750}) for args in _SIMULATOR_UPLOAD_JOB_PARAMS]
    + [(*args, {"0x0": 400, "0x3": 400}) for args in _SIMULATOR_UPLOAD_JOB_PARAMS_2Q]
    # the bell state of 00 and 11 == ~512
)

_ALL_INVALID_UPLOAD_JOB_PARAMS = [
    (client, redis, rq_worker, job)
    for job in _INVALID_JOBS_FOR_UPLOAD
    for client, redis, rq_worker in CLIENT_AND_RQ_WORKER_TUPLES
]

_FETCH_JOB_PARAMS = [
    (client, redis_client, job_id)
    for job_id in _JOB_IDS
    for client, redis_client in CLIENTS
]
_UNAUTHORIZED_FETCH_JOB_PARAMS = [
    (client, redis_client, job_id, get_headers(app_token), app_token)
    for job_id, app_token in zip_longest(_JOB_IDS, _WRONG_APP_TOKENS, fillvalue=None)
    for client, redis_client in CLIENTS
]
_UNAUTHENTICATED_UPLOAD_JOB_PARAMS = [
    (client, redis_client, rq_worker, job, get_headers(app_token), app_token)
    for job, app_token in zip_longest(
        _JOBS_FOR_UPLOAD, _WRONG_APP_TOKENS[:1], fillvalue=None
    )
    for client, redis_client, rq_worker in CLIENT_AND_RQ_WORKER_TUPLES
]
_STATIC_PROPERTIES_PARAMS = [
    (client, resp) for client, resp in zip(FASTAPI_CLIENTS, _STATIC_PROPERTIES)
]
_DYNAMIC_PROPERTIES_PARAMS = [
    (client, resp) for client, resp in zip(FASTAPI_CLIENTS, _DYNAMIC_PROPERTIES)
]
_ROOT_PARAMS = [
    (client, headers)
    for client in FASTAPI_CLIENTS
    for headers in create_invalid_mss_headers()
]


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


# @pytest.mark.parametrize("client, redis_client", CLIENTS)
# def test_fetch_all_jobs(redis_client, client):
#     """Get to /jobs returns all jobs"""
#     raw_jobs = _get_raw_jobs()
#
#     insert_in_hash(
#         client=redis_client,
#         hash_name=_JOBS_HASH_NAME,
#         data=raw_jobs,
#         id_fields=(_JOB_ID_FIELD,),
#     )
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         response = client.get("/jobs")
#         got = order_by(response.json(), field="job_id")
#         expected = order_by(raw_jobs, field="job_id")
#
#         assert response.status_code == 200
#         assert got == expected

#
# @pytest.mark.parametrize("client, redis_client, job_id", _FETCH_JOB_PARAMS)
# def test_fetch_job(redis_client, client, job_id: str, app_token_header):
#     """Get to /jobs/{job_id} returns the job for the given job_id"""
#     raw_jobs = _get_raw_jobs()
#
#     insert_in_hash(
#         client=redis_client,
#         hash_name=_JOBS_HASH_NAME,
#         data=raw_jobs,
#         id_fields=(_JOB_ID_FIELD,),
#     )
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         response = client.get(f"/jobs/{job_id}", headers=app_token_header)
#         got = response.json()
#         expected = {
#             "message": list(filter(lambda x: x["job_id"] == job_id, raw_jobs))[0]
#         }
#
#         assert response.status_code == 200
#         assert got == expected


# @pytest.mark.parametrize(
#     "client, redis_client, job_id, headers, app_token", _UNAUTHORIZED_FETCH_JOB_PARAMS
# )
# def test_unauthenticated_fetch_job(
#     redis_client, client, job_id: str, headers, app_token
# ):
#     """Get to /jobs/{job_id} raise 401 if no app token is passed in Authorization header"""
#     raw_jobs = _get_raw_jobs()
#     insert_in_hash(
#         client=redis_client,
#         hash_name=_JOBS_HASH_NAME,
#         data=raw_jobs,
#         id_fields=(_JOB_ID_FIELD,),
#     )
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         response = client.get(f"/jobs/{job_id}", headers=headers)
#         got = response.json()
#         detail = (
#             "Unauthorized"
#             if app_token is None
#             else f"job {job_id} does not exist for current user"
#         )
#         expected = {"detail": detail}
#
#         assert response.status_code == 401
#         assert got == expected


# @pytest.mark.parametrize("client, redis_client, job_id", _FETCH_JOB_PARAMS)
# def test_fetch_job_result(redis_client, client, job_id: str, app_token_header):
#     """Get to /jobs/{job_id}/result returns the job result for the given job_id"""
#     raw_jobs = _get_raw_jobs()
#
#     insert_in_hash(
#         client=redis_client,
#         hash_name=_JOBS_HASH_NAME,
#         data=raw_jobs,
#         id_fields=(_JOB_ID_FIELD,),
#     )
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         response = client.get(f"/jobs/{job_id}/result", headers=app_token_header)
#         got = response.json()
#         expected_job = list(filter(lambda x: x["job_id"] == job_id, raw_jobs))[0]
#
#         try:
#             expected = {"message": expected_job["result"]}
#         except KeyError:
#             expected = {"message": "job has not finished"}
#
#         assert response.status_code == 200
#         assert got == expected


# @pytest.mark.parametrize(
#     "client, redis_client, job_id, headers, app_token", _UNAUTHORIZED_FETCH_JOB_PARAMS
# )
# def test_unauthenticated_fetch_job_result(
#     redis_client, client, job_id: str, headers, app_token
# ):
#     """Get to /jobs/{job_id}/result returns 401 error when no valid app token is passed"""
#     raw_jobs = _get_raw_jobs()
#
#     insert_in_hash(
#         client=redis_client,
#         hash_name=_JOBS_HASH_NAME,
#         data=raw_jobs,
#         id_fields=(_JOB_ID_FIELD,),
#     )
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         response = client.get(f"/jobs/{job_id}/result", headers=headers)
#         got = response.json()
#         detail = (
#             "Unauthorized"
#             if app_token is None
#             else f"job {job_id} does not exist for current user"
#         )
#         expected = {"detail": detail}
#
#         assert response.status_code == 401
#         assert got == expected
#
#
# @pytest.mark.parametrize("client, redis_client, job_id", _FETCH_JOB_PARAMS)
# def test_fetch_job_status(redis_client, client, job_id: str, app_token_header):
#     """Get to /jobs/{job_id}/status returns the job status for the given job_id"""
#     raw_jobs = _get_raw_jobs()
#
#     insert_in_hash(
#         client=redis_client,
#         hash_name=_JOBS_HASH_NAME,
#         data=raw_jobs,
#         id_fields=(_JOB_ID_FIELD,),
#     )
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         response = client.get(f"/jobs/{job_id}/status", headers=app_token_header)
#         got = response.json()
#         expected_job = list(filter(lambda x: x["job_id"] == job_id, raw_jobs))[0]
#         # We add this, because we do not want to overwrite values as in the lines below
#         expected_job = copy.deepcopy(expected_job)
#
#         try:
#             status = expected_job["status"]
#             expected = {"message": status}
#         except KeyError:
#             expected = {"message": f"job {job_id} not found"}
#
#         assert response.status_code == 200
#         assert got == expected


# @pytest.mark.parametrize(
#     "client, redis_client, job_id, headers, app_token", _UNAUTHORIZED_FETCH_JOB_PARAMS
# )
# def test_unauthenticated_fetch_job_status(
#     redis_client, client, job_id: str, headers, app_token
# ):
#     """Get to /jobs/{job_id}/status returns 401 error when no valid app token is passed"""
#     raw_jobs = _get_raw_jobs()
#
#     insert_in_hash(
#         client=redis_client,
#         hash_name=_JOBS_HASH_NAME,
#         data=raw_jobs,
#         id_fields=(_JOB_ID_FIELD,),
#     )
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         response = client.get(f"/jobs/{job_id}/status", headers=headers)
#         got = response.json()
#         detail = (
#             "Unauthorized"
#             if app_token is None
#             else f"job {job_id} does not exist for current user"
#         )
#         expected = {"detail": detail}
#
#         assert response.status_code == 401
#         assert got == expected


# @pytest.mark.parametrize(
#     "client, redis_client, rq_worker, job, expected_counts", _ALL_UPLOAD_JOB_PARAMS
# )
# def test_upload_job(
#     client,
#     redis_client,
#     jobs_folder,
#     rq_worker,
#     job,
#     expected_counts,
#     app_token_header,
#     freezer,
# ):
#     """POST to '/jobs' uploads a new job"""
#     job_id = job[_JOB_ID_FIELD]
#     job_file_path = _save_job_file(folder=jobs_folder, job=job)
#     timestamp = MOCK_NOW.replace("+00:00", "Z")
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         with open(job_file_path, "rb") as file:
#             response = client.post(
#                 "/jobs", files={"upload_file": file}, headers=app_token_header
#             )
#
#         got = response.json()
#         expected = {"message": job_id}
#         expected_job_in_redis = {
#             "job_id": job_id,
#             "device": os.environ["DEFAULT_PREFIX"],
#             "download_url": f"http://localhost:8000/logfiles/{job_id}",
#             "failure_reason": None,
#             "cancellation_reason": None,
#             "status": "successful",
#             "stage": 9,
#             "timestamps": {
#                 _REGISTRATION_STAGE: {"started": timestamp, "finished": timestamp},
#                 _PRE_PROCESSING_STAGE: None,
#                 _EXECUTION_STAGE: {"started": timestamp, "finished": timestamp},
#                 _POST_PROCESSING_STAGE: {"started": timestamp, "finished": timestamp},
#                 _FINAL_STAGE: {"started": timestamp, "finished": timestamp},
#             },
#             "result": {"memory": [["0x0"] * 1024]},
#             "created_at": timestamp,
#             "updated_at": timestamp,
#         }
#
#         assert response.status_code == 200
#         assert got == expected
#
#         rq_worker.work(burst=True)
#         raw_job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
#         job_in_redis = json.loads(raw_job_in_redis)
#
#         # remove calibration date because it runs outside date freezer
#         job_in_redis.pop("calibration_date")
#
#         # Remove the result, because it is probabilistic
#         results = job_in_redis.pop("result")
#         expected_job_in_redis.pop("result")
#
#         # Check whether the result is plausible
#         # This can be seen as testing gate fidelity ~70%
#         assert _job_results_match(
#             results=results["memory"][0], expected_min_counts=expected_counts
#         )
#         assert job_in_redis == expected_job_in_redis


# @pytest.mark.parametrize(
#     "client, redis_client, rq_worker, job", _ALL_INVALID_UPLOAD_JOB_PARAMS
# )
# def test_upload_invalid_job(
#     client,
#     redis_client,
#     rq_worker,
#     job,
#     jobs_folder,
#     app_token_header,
#     freezer,
# ):
#     """POSTing an invalid structure of a job to '/jobs' fails"""
#     job_id = job.get(_JOB_ID_FIELD, "dummy")
#     job_file_path = _save_job_file(folder=jobs_folder, job=job)
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         with open(job_file_path, "rb") as file:
#             response = client.post(
#                 "/jobs", files={"upload_file": file}, headers=app_token_header
#             )
#
#         got = response.json()
#
#         assert response.status_code == 400
#         assert got["detail"].startswith("Invalid file: ")
#
#         rq_worker.work(burst=True)
#         raw_job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
#         assert raw_job_in_redis is None


# @pytest.mark.parametrize(
#     "client, redis_client, rq_worker, job, headers, app_token",
#     _UNAUTHENTICATED_UPLOAD_JOB_PARAMS,
# )
# def test_unauthenticated_upload_job(
#     client, redis_client, jobs_folder, rq_worker, job, headers, app_token
# ):
#     """POST to '/jobs' uploads a new job"""
#     job_id = job[_JOB_ID_FIELD]
#     job_file_path = _save_job_file(folder=jobs_folder, job=job)
#
#     # using context manager to ensure on_startup runs
#     with client as client:
#         with open(job_file_path, "rb") as file:
#             response = client.post(
#                 "/jobs", files={"upload_file": file}, headers=headers
#             )
#
#         rq_worker.work(burst=True)
#         raw_job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
#         got = response.json()
#         detail = (
#             "Unauthorized"
#             if app_token is None
#             else f"job {job_id} does not exist for current user"
#         )
#         expected = {"detail": detail}
#
#         assert response.status_code == 401
#         assert got == expected
#         assert raw_job_in_redis is None


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_duplicate_job_upload(
    client, redis_client, jobs_folder, rq_worker, job, app_token_header
):
    job_id = job[_JOB_ID_FIELD]
    job_file_path = _save_job_file(folder=jobs_folder, job=job)

    with client as client:
        with open(job_file_path, "rb") as file:
            first_response = client.post(
                "/jobs", files={"upload_file": file}, headers=app_token_header
            )
            # run the registration tasks
            rq_worker.work(burst=True)

            second_response = client.post(
                "/jobs", files={"upload_file": file}, headers=app_token_header
            )
            # run the registration tasks
            rq_worker.work(burst=True)

    assert first_response.status_code == 200
    assert second_response.status_code == 409


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_remove_job(
    client, redis_client, jobs_folder, rq_worker, job, app_token_header
):
    """DELETE to '/jobs/{job_id}' deletes the given job"""
    job_id = job[_JOB_ID_FIELD]
    job_file_path = _save_job_file(folder=jobs_folder, job=job)

    # using context manager to ensure on_startup runs
    with client as client:
        with open(job_file_path, "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=app_token_header
            )
            assert response.status_code == 200

        # start the job registration but stop there
        rq_worker.work(burst=True, max_jobs=1)
        # initiate delete
        deletion_response = client.delete(f"/jobs/{job_id}", headers=app_token_header)
        # run the rest of the tasks
        rq_worker.work(burst=True)

        job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
        assert deletion_response.status_code == 200
        assert job_in_redis is None


@pytest.mark.parametrize(
    "client, redis_client, rq_worker, job, headers, app_token",
    _UNAUTHENTICATED_UPLOAD_JOB_PARAMS,
)
def test_unauthenticated_remove_job(
    client,
    redis_client,
    jobs_folder,
    rq_worker,
    job: dict,
    app_token_header,
    headers,
    app_token,
):
    """Delete to /jobs/{job_id} returns 401 error when no valid app token is passed"""
    job_id = job[_JOB_ID_FIELD]
    job_file_path = _save_job_file(folder=jobs_folder, job=job)

    # using context manager to ensure on_startup runs
    with client as client:
        with open(job_file_path, "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=app_token_header
            )
            assert response.status_code == 200

        # start the job registration but stop there
        rq_worker.work(burst=True, max_jobs=1)
        # initiate delete
        deletion_response = client.delete(f"/jobs/{job_id}", headers=headers)
        # run the rest of the tasks
        rq_worker.work(burst=True)

        got = deletion_response.json()
        detail = (
            "Unauthorized"
            if app_token is None
            else f"job {job_id} does not exist for current user"
        )
        expected = {"detail": detail}

        job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
        assert deletion_response.status_code == 401
        assert got == expected
        assert job_in_redis is not None


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_cancel_job(
    client, redis_client, jobs_folder, rq_worker, job, app_token_header
):
    """POST to '/jobs/{job_id}/cancel' cancels the given job"""
    job_id = job[_JOB_ID_FIELD]
    job_file_path = _save_job_file(folder=jobs_folder, job=job)
    cancellation_reason = "just testing"
    timestamp = MOCK_NOW.replace("+00:00", "Z")

    # using context manager to ensure on_startup runs
    with client as client:
        with open(job_file_path, "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=app_token_header
            )
            assert response.status_code == 200

        # start the job registration but stop there
        rq_worker.work(burst=True, max_jobs=1)
        # initiate delete
        cancellation_response = client.post(
            f"/jobs/{job_id}/cancel",
            json=cancellation_reason,
            headers=app_token_header,
        )
        # run the rest of the tasks
        rq_worker.work(burst=True)

        expected_job_in_redis = {
            "job_id": job_id,
            "device": os.environ["DEFAULT_PREFIX"],
            "download_url": None,
            "failure_reason": None,
            "cancellation_reason": cancellation_reason,
            "status": "cancelled",
            "stage": 4,
            "timestamps": {
                _REGISTRATION_STAGE: {"started": timestamp, "finished": timestamp},
                _PRE_PROCESSING_STAGE: None,
                _EXECUTION_STAGE: None,
                _POST_PROCESSING_STAGE: None,
                _FINAL_STAGE: None,
            },
            "result": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

        raw_job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
        job_in_redis = json.loads(raw_job_in_redis)

        # remove calibration date because it runs outside date freezer
        job_in_redis.pop("calibration_date")

        assert cancellation_response.status_code == 200
        assert job_in_redis == expected_job_in_redis


@pytest.mark.parametrize(
    "client, redis_client, rq_worker, job, headers, app_token",
    _UNAUTHENTICATED_UPLOAD_JOB_PARAMS,
)
def test_unauthenticated_cancel_job(
    client,
    redis_client,
    jobs_folder,
    rq_worker,
    job,
    app_token_header,
    headers,
    app_token,
):
    """POST to '/jobs/{job_id}/cancel' returns 401 error when no valid app token is passed"""
    job_id = job[_JOB_ID_FIELD]
    job_file_path = _save_job_file(folder=jobs_folder, job=job)
    cancellation_reason = "just testing"

    # using context manager to ensure on_startup runs
    with client as client:
        with open(job_file_path, "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=app_token_header
            )
            assert response.status_code == 200

        # start the job registration but stop there
        rq_worker.work(burst=True, max_jobs=1)
        # initiate delete
        cancellation_response = client.post(
            f"/jobs/{job_id}/cancel",
            json=cancellation_reason,
            headers=headers,
        )
        # run the rest of the tasks
        rq_worker.work(burst=True)

        got = cancellation_response.json()
        detail = (
            "Unauthorized"
            if app_token is None
            else f"job {job_id} does not exist for current user"
        )
        expected = {"detail": detail}

        job_in_redis = json.loads(redis_client.hget(_JOBS_HASH_NAME, job_id))
        assert cancellation_response.status_code == 401
        assert got == expected
        assert job_in_redis["status"] == "successful"


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_download_logfile(
    logfile_download_folder, client, redis_client, rq_worker, job, app_token_header
):
    """GET to '/logfiles/{logfile_id}' downloads the given logfile"""
    job_id = job[_JOB_ID_FIELD]
    _save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get(f"/logfiles/{job_id}", headers=app_token_header)
        file_content = json.loads(response.content)
        assert response.status_code == 200
        assert file_content == job


@pytest.mark.parametrize(
    "client, redis_client, _, job, headers, app_token",
    _UNAUTHENTICATED_UPLOAD_JOB_PARAMS,
)
def test_unauthenticated_download_logfile(
    logfile_download_folder, client, redis_client, _, job, headers, app_token
):
    """Unauthenticated GET to '/logfiles/{logfile_id}' raises 401 error"""
    job_id = job[_JOB_ID_FIELD]
    _save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get(f"/logfiles/{job_id}", headers=headers)
        got = response.json()
        detail = (
            "Unauthorized"
            if app_token is None
            else f"job {job_id} does not exist for current user"
        )
        expected = {"detail": detail}

        assert response.status_code == 401
        assert got == expected


@pytest.mark.parametrize("client, expected", _STATIC_PROPERTIES_PARAMS)
def test_get_static_properties(client, expected):
    """Get to '/static-properties' retrieves the current static properties of the backend in v2 form"""
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/static-properties")
        got = response.json()
        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client, expected", _DYNAMIC_PROPERTIES_PARAMS)
def test_get_dynamic_properties(client, expected):
    """Get to '/dynamic-properties' retrieves the calibrated device parameters in version 2 form"""
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/dynamic-properties")
        got = response.json()
        assert response.status_code == 200
        assert _remove_dates(got) == expected


def _save_job_file(folder: Path, job: Dict[str, Any], ext: str = ".json") -> Path:
    """Saves the given job to a file and returns the Path

    It uses 'dummy' as a default job id

    Args:
        folder: the folder to save the job in
        job: the job to save

    Returns:
        the path where the job was saved
    """
    job_id = job.get(_JOB_ID_FIELD, "dummy")
    file_path = folder / f"{job_id}{ext}"

    with open(file_path, "w") as file:
        json.dump(job, file)

    return file_path


# FIXME: How do we simplify this?
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


def _remove_dates(dynamic_properties: Dict[str, Any]) -> Dict[str, Any]:
    """A utility to do away with the dates in the dynamic properties

    Args:
        dynamic_properties: the properties to change

    Returns:
        the dynamic properties with "date" and "last_calibrated" replaced with "NOW-PLACEHOLDER"
    """
    now_placeholder = "NOW-PLACEHOLDER"
    qubits = dynamic_properties.get("qubits", [])
    couplers = dynamic_properties.get("couplers", [])
    resonators = dynamic_properties.get("resonators", [])
    return {
        **dynamic_properties,
        "last_calibrated": now_placeholder,
        "qubits": [
            {
                k: v if not isinstance(v, dict) else {**v, "date": now_placeholder}
                for k, v in qubit_conf.items()
            }
            for qubit_conf in qubits
        ],
        "couplers": [
            {
                k: v if not isinstance(v, dict) else {**v, "date": now_placeholder}
                for k, v in coupler_conf.items()
            }
            for coupler_conf in couplers
        ],
        "resonators": [
            {
                k: v if not isinstance(v, dict) else {**v, "date": now_placeholder}
                for k, v in resonator_conf.items()
            }
            for resonator_conf in resonators
        ],
    }


def _get_raw_jobs() -> List[Dict[str, Any]]:
    """Prepares the raw jobs and returns them

    Returns:
        the list of job data as would be saved in the database
    """
    raw_jobs = with_incremental_timestamps(
        _JOBS_LIST, fields=["created_at", "calibration_date"]
    )
    return with_current_timestamps(raw_jobs, fields=["updated_at"])
