import json
import shutil
from os import path
from pathlib import Path
from typing import Any, Dict

import h5py
import pytest
import redis
from rq import Worker

from app.tests.conftest import CLIENT_AND_RQ_WORKER_TUPLES, CLIENTS, MOCK_NOW
from app.tests.utils.env import (
    TEST_MSS_MACHINE_ROOT_URL,
    TEST_QUANTIFY_MACHINE_ROOT_URL,
    TEST_STORAGE_ROOT,
)
from app.tests.utils.fixtures import get_fixture_path, load_json_fixture
from app.tests.utils.redis import insert_in_hash

_PARENT_FOLDER = path.dirname(path.abspath(__file__))
_JOBS_LIST = load_json_fixture("job_list.json")
_DEFAULT_LOGFILE_PATH = get_fixture_path("logfile.hdf5")
_BACKEND_PROPERTIES = load_json_fixture("backend_properties.json")
_JOBS_FOR_UPLOAD = load_json_fixture("jobs_to_upload.json")
_JOB_ID_FIELD = "job_id"
_JOB_IDS = [item[_JOB_ID_FIELD] for item in _JOBS_LIST]
_JOBS_HASH_NAME = "job_supervisor"
_DUMMY_JSON = {
    "foo": "bar",
    "os": "system",
}

# params
_UPLOAD_JOB_PARAMS = [
    (client, redis_client, rq_worker, job)
    for job in _JOBS_FOR_UPLOAD
    for client, redis_client, rq_worker in CLIENT_AND_RQ_WORKER_TUPLES
]
_FETCH_JOB_PARAMS = [
    (client, redis_client, job_id)
    for job_id in _JOB_IDS
    for client, redis_client in CLIENTS
]


@pytest.mark.parametrize("client, _", CLIENTS)
def test_root(client, _):
    """GET / returns "message": "Welcome to BCC machine"""
    with client as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "Welcome to BCC machine"}


@pytest.mark.parametrize("client, redis_client", CLIENTS)
def test_fetch_all_jobs(redis_client, client):
    """Get to /jobs returns tall jobs"""
    insert_in_hash(
        client=redis_client,
        hash_name=_JOBS_HASH_NAME,
        data=_JOBS_LIST,
        id_field=_JOB_ID_FIELD,
    )

    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/jobs")
        got = response.json()
        expected = {item[_JOB_ID_FIELD]: item for item in _JOBS_LIST}

        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client, redis_client, job_id", _FETCH_JOB_PARAMS)
def test_fetch_job(redis_client, client, job_id: str):
    """Get to /jobs/{job_id} returns the job for the given job_id"""
    insert_in_hash(
        client=redis_client,
        hash_name=_JOBS_HASH_NAME,
        data=_JOBS_LIST,
        id_field=_JOB_ID_FIELD,
    )

    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get(f"/jobs/{job_id}")
        got = response.json()
        expected = {
            "message": list(filter(lambda x: x["job_id"] == job_id, _JOBS_LIST))[0]
        }

        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client, redis_client, job_id", _FETCH_JOB_PARAMS)
def test_fetch_job_result(redis_client, client, job_id: str):
    """Get to /jobs/{job_id}/result returns the job result for the given job_id"""
    insert_in_hash(
        client=redis_client,
        hash_name=_JOBS_HASH_NAME,
        data=_JOBS_LIST,
        id_field=_JOB_ID_FIELD,
    )

    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get(f"/jobs/{job_id}/result")
        got = response.json()
        expected_job = list(filter(lambda x: x["job_id"] == job_id, _JOBS_LIST))[0]

        try:
            expected = {"message": expected_job["result"]}
        except KeyError:
            expected = {"message": "job has not finished"}

        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client, redis_client, job_id", _FETCH_JOB_PARAMS)
def test_fetch_job_status(redis_client, client, job_id: str):
    """Get to /jobs/{job_id}/status returns the job status for the given job_id"""
    # importing this here so that patching of redis.Redis does not get messed up
    # as it would if the import statement was at the beginning of the file.
    # FIXME: In future, the global `red = redis.Redis()` scattered in the code should be deleted
    from app.services.jobs.service import STR_LOC, Location

    insert_in_hash(
        client=redis_client,
        hash_name=_JOBS_HASH_NAME,
        data=_JOBS_LIST,
        id_field=_JOB_ID_FIELD,
    )

    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get(f"/jobs/{job_id}/status")
        got = response.json()
        expected_job = list(filter(lambda x: x["job_id"] == job_id, _JOBS_LIST))[0]

        try:
            status = expected_job["status"]
            status["location"] = STR_LOC[Location(status["location"])]
            expected = {"message": status}
        except KeyError:
            expected = {"message": f"job {job_id} not found"}

        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_upload_job(client, redis_client, client_jobs_folder, rq_worker, job):
    """POST to '/jobs' uploads a new job"""
    job_id = job[_JOB_ID_FIELD]
    job_file_path = _save_job_file(folder=client_jobs_folder, job=job)
    timestamp = MOCK_NOW.replace("+00:00", "Z")

    # using context manager to ensure on_startup runs
    with client as client:
        with open(job_file_path, "rb") as file:
            response = client.post("/jobs", files={"upload_file": file})

        got = response.json()
        expected = {"message": job_id}
        expected_job_in_redis = {
            "id": job_id,
            "priorities": {
                "global": 0,
                "local": {"pre_processing": 0, "execution": 0, "post_processing": 0},
            },
            "status": {
                "location": 4 if job["name"] == "pulse_schedule" else 5,
                "started": timestamp,
                "finished": None,
                "cancelled": {"time": None, "reason": None},
                "failed": {"time": None, "reason": None},
            },
            "result": None,
            "name": job["name"],
            "post_processing": job["post_processing"],
            "is_calibration_supervisor_job": job["is_calibration_supervisor_job"],
        }

        rq_worker.work(burst=True)
        raw_job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
        job_in_redis = json.loads(raw_job_in_redis)
        assert response.status_code == 200
        assert got == expected
        assert job_in_redis == expected_job_in_redis


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_remove_job(client, redis_client, client_jobs_folder, rq_worker, job):
    """DELETE to '/jobs/{job_id}' deletes the given job"""
    job_id = job[_JOB_ID_FIELD]
    job_file_path = _save_job_file(folder=client_jobs_folder, job=job)

    # using context manager to ensure on_startup runs
    with client as client:
        with open(job_file_path, "rb") as file:
            response = client.post("/jobs", files={"upload_file": file})
            assert response.status_code == 200

        # start the job registration but stop there
        rq_worker.work(burst=True, max_jobs=1)
        # initiate delete
        deletion_response = client.delete(f"/jobs/{job_id}")
        # run the rest of the tasks
        rq_worker.work(burst=True)

        job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
        assert deletion_response.status_code == 200
        assert job_in_redis is None


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_cancel_job(client, redis_client, client_jobs_folder, rq_worker, job):
    """POST to '/jobs/{job_id}/cancel' cancels the given job"""
    job_id = job[_JOB_ID_FIELD]
    job_file_path = _save_job_file(folder=client_jobs_folder, job=job)
    cancellation_reason = "just testing"
    timestamp = MOCK_NOW.replace("+00:00", "Z")

    # using context manager to ensure on_startup runs
    with client as client:
        with open(job_file_path, "rb") as file:
            response = client.post("/jobs", files={"upload_file": file})
            assert response.status_code == 200

        # start the job registration but stop there
        rq_worker.work(burst=True, max_jobs=1)
        # initiate delete
        cancellation_response = client.post(
            f"/jobs/{job_id}/cancel", json=cancellation_reason
        )
        # run the rest of the tasks
        rq_worker.work(burst=True)

        expected_job_in_redis = {
            "id": job_id,
            "priorities": {
                "global": 0,
                "local": {"pre_processing": 0, "execution": 0, "post_processing": 0},
            },
            "status": {
                "location": 2,
                "started": timestamp,
                "finished": None,
                "cancelled": {"time": timestamp, "reason": cancellation_reason},
                "failed": {"time": None, "reason": None},
            },
            "result": None,
            "name": job["name"],
            "post_processing": job["post_processing"],
            "is_calibration_supervisor_job": job["is_calibration_supervisor_job"],
        }

        raw_job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
        job_in_redis = json.loads(raw_job_in_redis)

        assert cancellation_response.status_code == 200
        assert job_in_redis == expected_job_in_redis


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_download_logfile(
    logfile_download_folder, client, redis_client, rq_worker, job
):
    """GET to '/logfiles/{logfile_id}' downloads the given logfile"""
    job_id = job[_JOB_ID_FIELD]
    _save_job_file(folder=logfile_download_folder, job=job, ext=".hdf5")

    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get(f"/logfiles/{job_id}")
        file_content = json.loads(response.content)
        assert response.status_code == 200
        assert file_content == job


@pytest.mark.parametrize("client, redis_client, rq_worker, job", _UPLOAD_JOB_PARAMS)
def test_upload_logfile(
    logfile_download_folder, client, redis_client, rq_worker, client_jobs_folder, job
):
    """POST to '/logfiles' uploads the given logfile"""

    job_id = job[_JOB_ID_FIELD]
    timestamp = MOCK_NOW.replace("+00:00", "Z")

    job_file_path = _save_job_file(folder=client_jobs_folder, job=job)
    logfile_path = _save_hdf5_logfile(folder=client_jobs_folder, job_id=job_id)

    # using context manager to ensure on_startup runs
    with client as client:
        with open(job_file_path, "rb") as file:
            response = client.post("/jobs", files={"upload_file": file})

        assert response.status_code == 200
        rq_worker.work(burst=True)

        with open(logfile_path, "rb") as file:
            response = client.post(
                "/logfiles",
                files={"upload_file": file},
                data={"logfile_type": "TQC_STORAGE"},
            )

        expected = {"message": "ok"}
        got = response.json()
        expected_job_in_redis = {
            "id": job_id,
            "priorities": {
                "global": 0,
                "local": {"pre_processing": 0, "execution": 0, "post_processing": 0},
            },
            "status": {
                "location": 9,
                "started": timestamp,
                "finished": timestamp,
                "cancelled": {"time": None, "reason": None},
                "failed": {"time": None, "reason": None},
            },
            "result": {"memory": [["0x0"] * 2000]},
            "name": job["name"],
            "post_processing": job["post_processing"],
            "is_calibration_supervisor_job": job["is_calibration_supervisor_job"],
        }

        rq_worker.work(burst=True)
        raw_job_in_redis = redis_client.hget(_JOBS_HASH_NAME, job_id)
        job_in_redis = json.loads(raw_job_in_redis)
        assert response.status_code == 200
        assert got == expected
        assert job_in_redis == expected_job_in_redis


@pytest.mark.parametrize("client, redis_client, rq_worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_get_rq_info(client, redis_client, rq_worker):
    """GET to '/rq-info' retrieves information about the running rq workers"""
    # using context manager to ensure on_startup runs
    with client as client:
        rq_worker.register_birth()
        response = client.get("/rq-info")
        got = response.json()
        workers = Worker.all(connection=redis_client)
        worker_info_list = [
            f"hostname: {worker.hostname},pid: {worker.pid}" for worker in workers
        ]
        expected = {"message": f"{{{''.join(worker_info_list)}}}"}
        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client, redis_client, job_id", _FETCH_JOB_PARAMS)
def test_call_rng(client, redis_client, job_id):
    """GET to '/rng/{job_id}' retrieves random numbers"""
    # using context manager to ensure on_startup runs
    with client as client:
        import requests

        response = client.get(f"/rng/{job_id}")
        assert response.status_code == 200
        assert response.text == '"Requesting RNG Numbers"'

        tmp_file = Path(TEST_STORAGE_ROOT) / f"{job_id}.to_quantify"
        mock_quantify_call_args = requests.post.call_args
        files_sent = mock_quantify_call_args.kwargs["files"]
        assert mock_quantify_call_args.args == (
            f"{TEST_QUANTIFY_MACHINE_ROOT_URL}/rng_LokiB",
        )
        assert files_sent["mss_url"] == (None, TEST_MSS_MACHINE_ROOT_URL)
        assert files_sent["upload_file"][0] == tmp_file.name

        with open(tmp_file, "r") as expected_file, open(
            files_sent["upload_file"][1].name
        ) as sent_file:
            assert sent_file.read() == expected_file.read()


@pytest.mark.parametrize("client, redis_client", CLIENTS)
def test_get_backend_properties(client, redis_client: redis.Redis):
    """Get to '/backend_properties' retrieves the current snapshot of the backend properties"""
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/backend_properties")
        got = response.json()
        assert response.status_code == 200
        assert got == _BACKEND_PROPERTIES


@pytest.mark.parametrize("client, redis_client", CLIENTS)
def test_get_snapshot(client, redis_client: redis.Redis):
    """Get to '/web-gui' retrieves the current snapshot of the backend properties"""
    redis_client.set("current_snapshot", json.dumps(_DUMMY_JSON))
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/web-gui")
        assert response.status_code == 200
        assert response.json() == _DUMMY_JSON


@pytest.mark.parametrize("client, redis_client", CLIENTS)
def test_web_config(client, redis_client: redis.Redis):
    """Get to '/web-gui/config' retrieves the config of this backend"""
    redis_client.set("config", json.dumps(_DUMMY_JSON))
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/web-gui/config")
        assert response.status_code == 200
        assert response.json() == _DUMMY_JSON


def _save_job_file(folder: Path, job: Dict[str, Any], ext: str = ".json") -> Path:
    """Saves the given job to a file and returns the Path

    Args:
        folder: the folder to save the job in
        job: the job to save

    Returns:
        the path where the job was saved
    """
    job_id = job[_JOB_ID_FIELD]
    file_path = folder / f"{job_id}{ext}"

    with open(file_path, "w") as file:
        json.dump(job, file)

    return file_path


def _save_hdf5_logfile(folder: Path, job_id: str) -> Path:
    """Saves the given job to a file and returns the Path

    Args:
        folder: the folder to save the job in
        job_id: the job id whose logfile is to be saved

    Returns:
        the path where the job was saved
    """
    file_path = folder / f"{job_id}.hdf5"
    shutil.copyfile(_DEFAULT_LOGFILE_PATH, file_path)

    with h5py.File(file_path, mode="r+") as file:
        file.attrs["job_id"] = job_id

    return file_path
