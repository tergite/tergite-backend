"""Tests for serving several devices from one process"""

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
