"""Tests for serving several devices from one process"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from services.external.mss import MssClient
from tests.utils.api import create_mss_headers, get_test_client
from tests.utils.devices import load_test_devices
from tests.utils.params import USERS


def test_load_devices_serves_each_device_settings(tmp_path, monkeypatch):
    """A device's settings are got by its name, the first device's by default"""
    import settings

    assert load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"]) == [
        "Alpha",
        "Beta",
    ]

    assert settings.get_device_settings().default_prefix == "Alpha"
    beta = settings.get_device_settings("Beta")
    assert (beta.default_prefix, beta.booking_db_url) == (
        "Beta",
        f"sqlite:///{tmp_path}/Beta.db",
    )


def test_bcc_env_file_names_each_device_an_api_worker_serves(tmp_path):
    env_files = []
    for name in ("Alpha", "Beta"):
        env_file = tmp_path / f"{name}.env"
        env_file.write_text(f"BCC_DEFAULT_PREFIX={name}\n")
        env_files.append(str(env_file))
    environ = {k: v for k, v in os.environ.items() if k != "BCC_DEFAULT_PREFIX"}

    result = subprocess.run(
        [sys.executable, "-c", "import settings; print(settings.get_device_names())"],
        cwd=Path(__file__).parents[1],
        env={**environ, "BCC_ENV_FILE": os.pathsep.join(env_files)},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "['Alpha', 'Beta']"


def test_app_keeps_each_device_resources_while_it_runs(
    tmp_path, monkeypatch, patched_mss_websockets
):
    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    from api import app

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
    from api import app

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


def test_an_env_file_from_before_redis_was_dropped_still_loads(tmp_path, monkeypatch):
    """A provisioned instance's backend.env keeps BCC_TUNER_REDIS_URL after an upgrade"""
    import settings

    file_values = {"BCC_TUNER_REDIS_URL": "redis://old:1/0", "BCC_REDIS_PORT": "6379"}
    load_test_devices(tmp_path, monkeypatch, ["sim"], file_values)

    assert settings.get_device_settings("sim").default_prefix == "sim"


def test_switching_a_device_off_leaves_the_others_on(tmp_path, monkeypatch, mocker):
    """/<device>/switch/off takes that device offline, and only it"""
    from api import app

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


def test_heavy_worker_waits_while_its_device_is_off(tmp_path, monkeypatch, mocker):
    import settings
    from api import app
    from services.scheduler import DeviceResources, close_device_resources
    from services.scheduler.dispatcher import Dispatcher, schedule_recalibration

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    mocker.patch.object(MssClient, "send_event")
    with get_test_client(app) as client:
        client.post("/Alpha/switch/off", headers=create_mss_headers(is_admin=True))
    devices = {
        name: DeviceResources.from_settings(settings.get_device_settings(name))
        for name in ("Alpha", "Beta")
    }
    # the API cancels its own as it stops
    for device in devices.values():
        schedule_recalibration(device, due_at=None, interval=60)

    is_alpha_run = Dispatcher(
        {"Alpha": devices["Alpha"]}, should_run_heavy=True, should_run_light=False
    ).run(is_burst=True, max_jobs=1)
    is_beta_run = Dispatcher(
        {"Beta": devices["Beta"]}, should_run_heavy=True, should_run_light=False
    ).run(is_burst=True, max_jobs=1)

    for device in devices.values():
        close_device_resources(device)
    assert not is_alpha_run
    assert is_beta_run


def test_each_device_has_its_own_jobs(tmp_path, monkeypatch):
    """A job stored for one device is not another's, though they share a database"""
    import settings
    from libs.queues.dtos import Job
    from services.scheduler import JobsStore
    from utils.sql_db import get_sql_engine, migrate_sql_db

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    db_url = f"sqlite:///{tmp_path}/shared.db"
    migrate_sql_db(db_url)
    db_engine = get_sql_engine(db_url)
    job = Job(device="Beta", calibration_date="")
    beta_jobs = JobsStore.from_settings(db_engine, settings.get_device_settings("Beta"))
    beta_jobs.insert(job)

    alpha_jobs = JobsStore.from_settings(
        db_engine, settings.get_device_settings("Alpha")
    )
    assert not alpha_jobs.is_stored(job.job_id)


def test_a_booking_on_one_device_leaves_the_others_free(tmp_path, monkeypatch, mocker):
    """Devices may share a database"""
    from api import app

    shared_db_url = f"sqlite:///{tmp_path}/shared.db"
    load_test_devices(
        tmp_path, monkeypatch, ["Alpha", "Beta"], {"BCC_BOOKING_DB_URL": shared_db_url}
    )
    mocker.patch.object(MssClient, "send_event")
    start_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    booking_info = {
        "start_utc": start_utc.isoformat(),
        "end_utc": (start_utc + timedelta(minutes=10)).isoformat(),
    }
    user_id = f"{uuid4()}"

    with get_test_client(app) as client:
        alpha_response = client.post(
            "/Alpha/bookings", json=booking_info, headers=create_mss_headers(user_id)
        )
        beta_response = client.post(
            "/Beta/bookings", json=booking_info, headers=create_mss_headers(user_id)
        )
        beta_bookings = client.get("/Beta/bookings", headers=create_mss_headers())

    assert (alpha_response.status_code, beta_response.status_code) == (200, 200)
    assert [booking["id"] for booking in beta_bookings.json()["data"]] == [
        beta_response.json()["id"]
    ]


def test_each_device_has_its_own_executor_lock(tmp_path, monkeypatch):
    import settings
    from services.scheduler import DeviceResources

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    alpha = DeviceResources.from_settings(settings.get_device_settings("Alpha"))
    beta = DeviceResources.from_settings(settings.get_device_settings("Beta"))

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
    tmp_path, monkeypatch, mocker, path
):
    from api import app
    from api.dependencies import (
        DeviceResourcesDep,
        get_bearer_token,
        get_verified_mss_admin_user_id,
        get_verified_mss_user_id,
    )
    from utils.exc import NotAuthenticatedError

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    reached_devices = []

    def record_device(device: DeviceResourcesDep):
        reached_devices.append(device.settings.default_prefix)
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
