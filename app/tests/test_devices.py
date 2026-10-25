"""Tests for serving several devices from one process"""

from uuid import uuid4

from app.tests.conftest import USERS
from app.tests.utils.api import create_mss_headers
from app.tests.utils.devices import load_test_devices


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


def test_each_device_has_its_own_queues(tmp_path, monkeypatch):
    """The queue pool and queue context are the current device's"""
    import settings
    from app.libs.queues.dtos import clear_queue_context, get_queue_context
    from app.services.scheduler.queues import clear_queue_pool, get_queue_pool

    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    try:
        with settings.using_device("Alpha"):
            alpha_pool, alpha_context = get_queue_pool(), get_queue_context()
        with settings.using_device("Beta"):
            beta_pool, beta_context = get_queue_pool(), get_queue_context()

        assert alpha_pool.general.name == "Alpha_general"
        assert beta_pool.general.name == "Beta_general"
        assert alpha_context["queue_prefix"] == "Alpha"
        assert beta_context["booking_db_url"] == f"sqlite:///{tmp_path}/Beta.db"
    finally:
        clear_queue_pool()
        clear_queue_context()


def test_each_device_path_reaches_that_device(tmp_path, monkeypatch):
    """/<device>/... is that device's API, and a path naming no device the default one's"""
    load_test_devices(tmp_path, monkeypatch, ["Alpha", "Beta"])
    from fastapi.testclient import TestClient

    from app.api import app

    client = TestClient(app)
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
