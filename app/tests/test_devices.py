"""Tests for serving several devices from one process"""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.external.mss import MssClient
from app.tests.utils.api import create_mss_headers, get_test_client
from app.tests.utils.devices import load_test_devices
from app.tests.utils.params import USERS


def test_load_devices_serves_each_device_settings(tmp_path, monkeypatch):
    """settings.X is the current device's X: the one using_device names, or else the first"""
    import settings

    assert load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"]) == [
        "Alpha",
        "Beta",
    ]

    assert settings.DEFAULT_PREFIX == "Alpha"
    with settings.using_device("Beta"):
        assert settings.DEFAULT_PREFIX == "Beta"
        assert settings.BOOKING_DB_URL == f"sqlite:///{tmp_path}/Beta.db"
    assert settings.DEFAULT_PREFIX == "Alpha"


def test_bcc_env_file_names_each_device_an_api_worker_serves(tmp_path):
    env_files = []
    for name in ("Alpha", "Beta"):
        env_file = tmp_path / f"{name}.env"
        env_file.write_text(f"BCC_DEFAULT_PREFIX={name}\n")
        env_files.append(str(env_file))
    environ = {k: v for k, v in os.environ.items() if k != "BCC_DEFAULT_PREFIX"}

    result = subprocess.run(
        [sys.executable, "-c", "import settings; print(settings.get_device_names())"],
        cwd=Path(__file__).parents[2],
        env={**environ, "BCC_ENV_FILE": os.pathsep.join(env_files)},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "['Alpha', 'Beta']"


def test_each_device_has_its_own_queues(tmp_path, monkeypatch):
    """Each device's queue pool has its own queues"""
    import settings
    from app.services.scheduler import QueuePool
    from app.utils.redis import get_redis_connection

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    connection = get_redis_connection(settings.get_device_settings().rq_redis_url)

    alpha_pool = QueuePool.from_settings(
        settings.get_device_settings("Alpha"), connection
    )
    beta_pool = QueuePool.from_settings(
        settings.get_device_settings("Beta"), connection
    )

    assert alpha_pool.general.name == "Alpha_general"
    assert beta_pool.general.name == "Beta_general"


def test_app_keeps_each_device_resources_while_it_runs(
    tmp_path, monkeypatch, patched_mss_websockets
):
    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    from app.api import app

    with get_test_client(app):
        device_resources = app.state.device_resources

        assert {
            name: resources.settings.default_prefix
            for name, resources in device_resources.items()
        } == {"Alpha": "Alpha", "Beta": "Beta"}
        assert (
            device_resources["Beta"].bookings_db.url.database == f"{tmp_path}/Beta.db"
        )


def test_each_device_path_reaches_that_device(tmp_path, monkeypatch, mocker):
    """/<device>/... is that device's API, and a path naming no device the default one's"""
    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    from app.api import app

    mocker.patch.object(MssClient, "send_event")
    with get_test_client(app) as client:
        user_id = f"{uuid4()}"

        response = client.post(
            "/Beta/users",
            json={**USERS[0], "id": user_id},
            headers=create_mss_headers(is_admin=True),
        )
        assert response.status_code == 200

        def get_user_ids(path: str) -> list[str]:
            # each request takes a fresh nonce
            response = client.get(path, headers=create_mss_headers(is_admin=True))
            return [user["id"] for user in response.json()["data"]]

        assert user_id in get_user_ids("/Beta/users")
        assert user_id not in get_user_ids("/Alpha/users")
        assert user_id not in get_user_ids("/users")


def test_worker_runs_each_job_as_its_queue_device(tmp_path, monkeypatch):
    """A queue is the device's whose name prefixes it, the longest if several do"""
    from app.api.worker import _get_device_of_queue

    load_test_devices(tmp_path, monkeypatch, ["sim", "sim_2q"])

    assert _get_device_of_queue("sim_general") == "sim"
    assert _get_device_of_queue("sim_2q_general") == "sim_2q"
    assert _get_device_of_queue("sim_2q_normal_execution") == "sim_2q"


def test_device_env_variables_override_the_shared_ones(tmp_path, monkeypatch):
    """BCC_<PREFIX>_<NAME> sets one device's NAME, over BCC_<NAME>; a bare NAME is not read"""
    import settings

    monkeypatch.setenv("BCC_MAX_IDLE_TIME", "7")
    monkeypatch.setenv("BCC_SIM_2Q_MAX_IDLE_TIME", "42")
    monkeypatch.delenv("BCC_MAX_SLOTS_PER_DAY", raising=False)
    monkeypatch.setenv("MAX_SLOTS_PER_DAY", "3")
    load_test_devices(tmp_path, monkeypatch, ["sim", "sim_2q"])

    assert settings.get_device_settings("sim").max_idle_time == 7
    assert settings.get_device_settings("sim_2q").max_idle_time == 42
    # the default, as MAX_SLOTS_PER_DAY is not BCC_ prefixed
    assert settings.get_device_settings("sim_2q").max_slots_per_day == 1


def test_env_file_keys_are_bcc_prefixed(tmp_path, monkeypatch):
    """An env file's BCC_<NAME> sets <NAME>, and a bare key is refused, not ignored"""
    import settings

    monkeypatch.delenv("BCC_PORT", raising=False)
    load_test_devices(tmp_path, monkeypatch, ["sim"], {"BCC_PORT": "8123"})
    assert settings.get_device_settings("sim").port == 8123

    with pytest.raises(ValueError, match="PORT must be written BCC_<NAME>"):
        load_test_devices(tmp_path, monkeypatch, ["sim"], {"PORT": "8123"})


def test_switching_a_device_off_leaves_the_others_on(
    tmp_path, monkeypatch, mocker, redis_client
):
    """/<device>/switch/off takes that device offline, and only it"""
    from app.api import app

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    mocker.patch.object(MssClient, "send_event")
    with get_test_client(app) as client:
        response = client.post(
            "/Alpha/switch/off", headers=create_mss_headers(is_admin=True)
        )
        assert response.status_code == 200

        alpha_status = client.get("/Alpha/switch/status", headers=create_mss_headers())
        beta_status = client.get("/Beta/switch/status", headers=create_mss_headers())
        assert alpha_status.json() == {"status": "OFF"}
        assert beta_status.json() == {"status": "ON"}


def test_heavy_worker_waits_while_its_device_is_off(
    tmp_path, monkeypatch, mocker, redis_client
):
    from rq import Queue
    from rq.worker import StopRequested

    import settings
    from app.api import app
    from app.api.worker import PreloadedRqWorker
    from app.services.scheduler import DeviceResources

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    mocker.patch.object(MssClient, "send_event")
    # the worker's start-up, but for building the executor and publishing the device
    mocker.patch(
        "app.api.worker.preload_worker_globals",
        side_effect=lambda should_load_executor: DeviceResources.from_settings(
            settings.get_device_settings()
        ),
    )
    alpha_worker = PreloadedRqWorker(
        [Queue("Alpha_preprocessing", connection=redis_client)],
        connection=redis_client,
    )
    beta_worker = PreloadedRqWorker(
        [Queue("Beta_preprocessing", connection=redis_client)],
        connection=redis_client,
    )
    with get_test_client(app) as client:
        client.post("/Alpha/switch/off", headers=create_mss_headers(is_admin=True))

    with pytest.raises(StopRequested):
        alpha_worker.check_for_suspension(True)
    beta_worker.check_for_suspension(True)


def test_each_device_has_its_own_jobs(tmp_path, monkeypatch, redis_client):
    """A job stored for one device is not another's, though they share a Redis"""
    import settings
    from app.libs.queues.dtos import Job
    from app.services.scheduler.store import get_jobs_store
    from app.utils.redis import get_redis_connection

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    job = Job(device="Beta", calibration_date="")
    with settings.using_device("Beta"):
        beta_jobs = get_jobs_store(
            get_redis_connection(settings.get_device_settings().rq_redis_url),
            settings.get_device_settings(),
        )
        beta_jobs.insert(job)

    with settings.using_device("Alpha"):
        alpha_jobs = get_jobs_store(
            get_redis_connection(settings.get_device_settings().rq_redis_url),
            settings.get_device_settings(),
        )
        assert not alpha_jobs.exists(job.job_id)


def test_each_device_has_its_own_executor_lock(tmp_path, monkeypatch):
    import settings
    from app.services.scheduler import DeviceResources

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    with settings.using_device("Alpha"):
        alpha = DeviceResources.from_settings(settings.get_device_settings())
    with settings.using_device("Beta"):
        beta = DeviceResources.from_settings(settings.get_device_settings())

    assert alpha.executor_lock.lock_file != beta.executor_lock.lock_file


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/static-properties",
        "/users",
        "/bookings",
        "/jobs",
        "/logfiles/7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "/recalibration/info",
        "/switch/status",
    ],
)
def test_each_route_group_reaches_the_device_its_path_names(
    tmp_path, monkeypatch, mocker, redis_client, path
):
    import settings
    from app.api import app
    from app.api.dependencies import (
        get_bearer_token,
        get_verified_mss_admin_user_id,
        get_verified_mss_user_id,
    )
    from app.utils.exc import NotAuthenticatedError

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    reached_devices = []

    def record_device():
        reached_devices.append(settings.DEFAULT_PREFIX)
        raise NotAuthenticatedError("only recording the device")

    monkeypatch.setitem(
        app.dependency_overrides, get_verified_mss_user_id, record_device
    )
    monkeypatch.setitem(
        app.dependency_overrides, get_verified_mss_admin_user_id, record_device
    )
    monkeypatch.setitem(app.dependency_overrides, get_bearer_token, record_device)
    mocker.patch.object(MssClient, "send_event")
    with get_test_client(app) as client:
        client.get(f"/Beta{path}")

    assert reached_devices == ["Beta"]
