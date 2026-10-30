"""Tests for the settings and configs"""

import importlib

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
from tests.utils.params import HAS_QUANTIFY
from utils.datetime import get_utc_now
from utils.exc import MssTimeoutError

_QUANTIFY_CONFIG_FILE = get_fixture_path("generic-quantify-config.json")
_QUANTIFY_METADATA_FILE = get_fixture_path("generic-quantify-config.yml")


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
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
    assert mock_sleep.call_count == expected_connect_calls


def test_calibration_seed_required_for_simulator(patched_mss_websockets, monkeypatch):
    """Raises validation errors if calibration seed is not set for simulator."""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "qiskit_pulse_1q")
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
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "qiskit_pulse_1q")
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


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
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


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
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


def _reload_settings() -> None:
    """Re-reads the settings from the env variables, as the app reads them at run time"""
    import settings

    importlib.reload(settings)
