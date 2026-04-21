"""Tests for the settings and configs"""

import os

import pytest
from pydantic import ValidationError

from app.libs.quantum_executor.utils.config import (
    QuantifyMetadata,
    load_quantify_config,
)
from app.tests.utils.env import (
    TEST_BACKEND_SETTINGS_FILE,
    TEST_BROKEN_QUANTIFY_CONFIG_FILE,
    TEST_BROKEN_QUANTIFY_METADATA_FILE,
    TEST_DEFAULT_PREFIX_SIM_1Q,
    TEST_QUANTIFY_CONFIG_FILE,
    TEST_QUANTIFY_METADATA_FILE,
    TEST_QUANTIFY_SEED_FILE,
    TEST_SIMQ1_BACKEND_SETTINGS_FILE,
)
from app.tests.utils.fixtures import get_fixture_path
from app.tests.utils.modules import remove_modules

_QUANTIFY_CONFIG_FILE = get_fixture_path("generic-quantify-config.json")
_QUANTIFY_METADATA_FILE = get_fixture_path("generic-quantify-config.yml")


def test_load_quantify_config_files():
    """ExecutorConfig can load YAML Quantify Metadata File and Quantify Config File"""
    conf_metadata = QuantifyMetadata.from_yaml(_QUANTIFY_METADATA_FILE)
    conf = load_quantify_config(_QUANTIFY_CONFIG_FILE)

    # Both configuration files are validated in QuantifyMetadata
    assert conf_metadata
    assert conf


@pytest.mark.asyncio
async def test_no_mss_connected():
    """Raises connection errors only when MSS is unavailable"""
    remove_modules(["os", "app", "settings"])

    os.environ["EXECUTOR_TYPE"] = "quantify"
    os.environ["BACKEND_SETTINGS"] = TEST_BACKEND_SETTINGS_FILE
    mss_port = os.getenv("UNAVAILABLE_MSS_PORT", "5050")
    os.environ["MSS_MACHINE_ROOT_URL"] = f"http://localhost:{mss_port}"

    from sqlmodel import SQLModel

    SQLModel.metadata.clear()

    with pytest.raises(TimeoutError, match=r"Connection to MSS took longer than"):
        from app.api import app

        # just to run the startup events
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_calibration_seed_required_for_simulator(patched_mss_websockets):
    """Raises validation errors if calibration seed is not set for simulator."""
    remove_modules(["os", "app", "settings"])

    os.environ["EXECUTOR_TYPE"] = "qiskit_pulse_1q"
    os.environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX_SIM_1Q
    os.environ["BACKEND_SETTINGS"] = TEST_SIMQ1_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = get_fixture_path("non-existent.toml")

    from sqlmodel import SQLModel

    SQLModel.metadata.clear()

    with pytest.raises(
        ValidationError, match="Calibration config is required for simulators."
    ):
        from app.api import app

        # just to run the startup events
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_calibration_seed_broken_for_simulator(patched_mss_websockets):
    """Raises validation errors if calibration seed provided is broken for simulator only."""
    remove_modules(["os", "app", "settings"])

    os.environ["EXECUTOR_TYPE"] = "qiskit_pulse_1q"
    os.environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX_SIM_1Q
    os.environ["BACKEND_SETTINGS"] = TEST_SIMQ1_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = get_fixture_path("broken.seed.toml")

    from sqlmodel import SQLModel

    SQLModel.metadata.clear()

    with pytest.raises(
        ValidationError, match="Calibration config is required for simulators."
    ):
        from app.api import app

        # just to run the startup events
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_quantify_metadata_is_broken(patched_mss_websockets):
    """Raises validation errors if quantify metadata conf file is broken"""
    remove_modules(["os", "app", "settings"])

    os.environ["EXECUTOR_TYPE"] = "quantify"
    os.environ["BACKEND_SETTINGS"] = TEST_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = TEST_QUANTIFY_SEED_FILE
    os.environ["QUANTIFY_CONFIG_FILE"] = TEST_QUANTIFY_CONFIG_FILE
    os.environ["QUANTIFY_METADATA_FILE"] = TEST_BROKEN_QUANTIFY_METADATA_FILE

    from sqlmodel import SQLModel

    SQLModel.metadata.clear()

    with pytest.raises(ValidationError):
        from app.api import app

        # just to run the startup events
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_quantify_config_is_broken(patched_mss_websockets):
    """Raises validation errors if quantify config file is broken"""
    remove_modules(["os", "app", "settings"])

    os.environ["EXECUTOR_TYPE"] = "quantify"
    os.environ["BACKEND_SETTINGS"] = TEST_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = TEST_QUANTIFY_SEED_FILE
    os.environ["QUANTIFY_CONFIG_FILE"] = TEST_BROKEN_QUANTIFY_CONFIG_FILE
    os.environ["QUANTIFY_METADATA_FILE"] = TEST_QUANTIFY_METADATA_FILE

    from sqlmodel import SQLModel

    SQLModel.metadata.clear()

    with pytest.raises(ValidationError):
        from app.api import app

        # just to run the startup events
        async with app.router.lifespan_context(app):
            pass
