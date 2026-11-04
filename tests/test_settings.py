"""Tests for the settings and configs"""

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import settings
from api import app
from services.external.mss import (
    DeviceEvent,
    DeviceEventName,
    DeviceSwitchData,
    MssClient,
    get_private_key,
)
from services.scheduler.utils import get_executor, get_executor_options
from tests.utils.env import (
    TEST_BACKEND_SETTINGS_FILE,
    TEST_BROKEN_QUANTIFY_CONFIG_FILE,
    TEST_BROKEN_QUANTIFY_METADATA_FILE,
    TEST_DEFAULT_PREFIX_SIM_1Q,
    TEST_QUANTIFY_CONFIG_FILE,
    TEST_QUANTIFY_METADATA_FILE,
    TEST_QUANTIFY_SEED_FILE,
    TEST_SIMQ1_BACKEND_SETTINGS_FILE,
)
from tests.utils.fixtures import get_fixture_path
from utils.datetime import get_utc_now
from utils.exc import MssTimeoutError

_QUANTIFY_CONFIG_FILE = get_fixture_path("generic-quantify-config.json")
_QUANTIFY_METADATA_FILE = get_fixture_path("generic-quantify-config.yml")


def test_load_quantify_config_files():
    """ExecutorConfig can load YAML Quantify Metadata File and Quantify Config File"""
    from libs.quantum_executor.quantify.utils.config import (
        QuantifyMetadata,
        load_quantify_config,
    )

    conf_metadata = QuantifyMetadata.from_yaml(_QUANTIFY_METADATA_FILE)
    conf = load_quantify_config(_QUANTIFY_CONFIG_FILE)

    # Both configuration files are validated in QuantifyMetadata
    assert conf_metadata
    assert conf


@pytest.fixture(autouse=True)
def _restore_settings(monkeypatch):
    """Undoes a test's env variables, and re-reads the settings from the rest"""
    yield
    monkeypatch.undo()
    _reload_settings()


@pytest.mark.timeout(30)
def test_mss_client_reconnects_as_often_as_the_settings_say(mocker, monkeypatch):
    """MSS_CONNECTION_MAX_ATTEMPTS is respected: connect is retried exactly that
    many times before an MssTimeoutError is raised.
    """
    connection_attempts = 2  # MSS_CONNECTION_MAX_ATTEMPTS
    monkeypatch.setenv("BCC_MSS_MACHINE_ROOT_URL", "http://localhost:19999")
    monkeypatch.setenv("BCC_MSS_CONNECTION_TIMEOUT", "0.05")
    monkeypatch.setenv("BCC_MSS_CONNECTION_MAX_ATTEMPTS", f"{connection_attempts}")
    # Large enough that the response-timeout path is never hit first.
    monkeypatch.setenv("BCC_MSS_RESPONSE_TIMEOUT", "3600")
    _reload_settings()
    mock_connect = mocker.patch(
        "services.external.mss.service.connect",
        side_effect=OSError("Connection refused"),
    )
    # Suppress actual sleeping so the test is instant.
    mock_sleep = mocker.patch("time.sleep")
    device_settings = settings.get_device_settings()
    private_key = get_private_key(
        device_settings.private_key_file, device_settings.private_key_password
    )
    mss_client = MssClient.from_settings(device_settings, private_key)
    switched_on = DeviceEvent(
        name=DeviceEventName.SWITCHED_ON, data=DeviceSwitchData(date=get_utc_now())
    )

    with pytest.raises(MssTimeoutError, match=r"maximum connection attempts"):
        mss_client.send_event(switched_on)

    expected_connect_calls = connection_attempts + 1
    assert mock_connect.call_count == expected_connect_calls
    # a delay between attempts, none after the last
    assert mock_sleep.call_count == expected_connect_calls - 1


@pytest.mark.timeout(30)
def test_mss_client_waits_no_longer_than_the_settings_say(mocker, monkeypatch):
    monkeypatch.setenv("BCC_MSS_MACHINE_ROOT_URL", "http://localhost:19999")
    monkeypatch.setenv("BCC_MSS_CONNECTION_TIMEOUT", "0.05")
    monkeypatch.setenv("BCC_MSS_CONNECTION_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("BCC_MSS_MAX_RECONNECTION_DELAY", "0.5")
    monkeypatch.setenv("BCC_MSS_RESPONSE_TIMEOUT", "3600")
    _reload_settings()
    mocker.patch(
        "services.external.mss.service.connect",
        side_effect=OSError("Connection refused"),
    )
    mock_sleep = mocker.patch("time.sleep")
    device_settings = settings.get_device_settings()
    private_key = get_private_key(
        device_settings.private_key_file, device_settings.private_key_password
    )
    mss_client = MssClient.from_settings(device_settings, private_key)
    switched_on = DeviceEvent(
        name=DeviceEventName.SWITCHED_ON, data=DeviceSwitchData(date=get_utc_now())
    )

    with pytest.raises(MssTimeoutError):
        mss_client.send_event(switched_on)

    assert [call.args[0] for call in mock_sleep.call_args_list] == [0.5, 0.5, 0.5]


def test_calibration_seed_required_for_simulator(patched_mss_websockets, monkeypatch):
    """Raises validation errors if calibration seed is not set for simulator."""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "quantify")
    monkeypatch.setenv("BCC_DEFAULT_PREFIX", TEST_DEFAULT_PREFIX_SIM_1Q)
    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_SIMQ1_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", get_fixture_path("non-existent.toml"))
    _reload_settings()

    with (
        pytest.raises(
            ValidationError, match="Calibration config is required for simulators."
        ),
        TestClient(app),
    ):
        pass


def test_calibration_seed_broken_for_simulator(patched_mss_websockets, monkeypatch):
    """Raises validation errors if calibration seed provided is broken for simulator only."""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "quantify")
    monkeypatch.setenv("BCC_DEFAULT_PREFIX", TEST_DEFAULT_PREFIX_SIM_1Q)
    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_SIMQ1_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", get_fixture_path("broken.seed.toml"))
    _reload_settings()

    with (
        pytest.raises(
            ValidationError, match="Calibration config is required for simulators."
        ),
        TestClient(app),
    ):
        pass


def test_quantify_metadata_is_broken(monkeypatch):
    """The heavy worker's executor build raises if the quantify metadata file is broken"""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "quantify")
    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", TEST_QUANTIFY_SEED_FILE)
    monkeypatch.setenv("BCC_QUANTIFY_CONFIG_FILE", TEST_QUANTIFY_CONFIG_FILE)
    monkeypatch.setenv("BCC_QUANTIFY_METADATA_FILE", TEST_BROKEN_QUANTIFY_METADATA_FILE)
    _reload_settings()

    with pytest.raises(ValidationError):
        get_executor(get_executor_options(settings.get_device_settings()))


def test_quantify_config_is_broken(monkeypatch):
    """The heavy worker's executor build raises if the quantify config file is broken"""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "quantify")
    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", TEST_QUANTIFY_SEED_FILE)
    monkeypatch.setenv("BCC_QUANTIFY_CONFIG_FILE", TEST_BROKEN_QUANTIFY_CONFIG_FILE)
    monkeypatch.setenv("BCC_QUANTIFY_METADATA_FILE", TEST_QUANTIFY_METADATA_FILE)
    _reload_settings()

    with pytest.raises(ValidationError):
        get_executor(get_executor_options(settings.get_device_settings()))


def test_executor_options_allow_resetting_the_clusters_when_set(monkeypatch):
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "quantify")
    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", TEST_QUANTIFY_SEED_FILE)
    monkeypatch.setenv("BCC_ARE_CLUSTERS_RESETTABLE", "true")
    _reload_settings()

    assert get_executor_options(settings.get_device_settings()).is_cluster_reset_allowed


@pytest.mark.parametrize("max_worker_wait_time", ["0", "-1"])
def test_max_worker_wait_time_must_be_above_zero(monkeypatch, max_worker_wait_time):
    """A worker waiting no time would look for work without pause"""
    monkeypatch.setenv("BCC_MAX_WORKER_WAIT_TIME", max_worker_wait_time)

    with pytest.raises(ValidationError, match="max_worker_wait_time"):
        _reload_settings()


@pytest.mark.parametrize("executor_type", ["qiskit_pulse_1q", "qiskit_pulse_2q"])
def test_executor_type_other_than_quantify_is_refused(monkeypatch, executor_type):
    """A simulated device is executor type quantify with a simulator section"""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", executor_type)

    with pytest.raises(ValidationError, match="executor_type"):
        _reload_settings()


def test_relative_paths_are_read_from_the_folder_the_settings_are_read_in(
    monkeypatch, tmp_path
):
    """A heavy worker then moves to its device's own folder, where they would point
    elsewhere"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BCC_QUANTIFY_CONFIG_FILE", "quantify-config.json")
    monkeypatch.setenv("BCC_CALIBRATION_SEED", "calibration.seed.toml")
    monkeypatch.setenv("BCC_BOOKING_DB_URL", "sqlite:///booking_db.db")

    _reload_settings()

    device = settings.get_device_settings()
    assert device.quantify_config_file == Path.cwd() / "quantify-config.json"
    assert device.calibration_seed == str(Path.cwd() / "calibration.seed.toml")
    assert device.booking_db_url == f"sqlite:///{Path.cwd() / 'booking_db.db'}"


@pytest.mark.parametrize(
    "url", ["sqlite:////var/lib/bcc/booking_db.db", "sqlite:///:memory:", "sqlite://"]
)
def test_absolute_and_memory_database_urls_stay_as_they_are(monkeypatch, url):
    monkeypatch.setenv("BCC_BOOKING_DB_URL", url)

    _reload_settings()

    assert settings.get_device_settings().booking_db_url == url


def _reload_settings() -> None:
    """Re-reads the settings from the env variables, as the app reads them at run time"""
    import settings

    importlib.reload(settings)
