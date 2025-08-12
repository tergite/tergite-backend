"""Tests for the settings and configs"""

import importlib
import os

import pytest
import requests
from pydantic import ValidationError

from app.libs.quantum_executor.utils.config import (
    QuantifyMetadata,
    load_quantify_config,
)
from app.tests.conftest import FASTAPI_CLIENTS
from app.tests.utils.env import (
    TEST_BACKEND_SETTINGS_FILE,
    TEST_BROKEN_QUANTIFY_CONFIG_FILE,
    TEST_BROKEN_QUANTIFY_METADATA_FILE,
    TEST_DEFAULT_PREFIX_SIM_1Q,
    TEST_MSS_APP_TOKEN,
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


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_authenticated_mss_client(client):
    """The MSS client used to make requests to MSS is authenticated"""
    from app.utils.api import get_mss_client

    mss_client = get_mss_client()
    authorization_header = mss_client.headers.get("Authorization")
    assert authorization_header == f"Bearer {TEST_MSS_APP_TOKEN}"


def test_redis_connection(redis_client):
    """The global redis client as be found in settings"""
    from settings import REDIS_CONNECTION

    # Write a value in the real redis connection
    expected = "123"
    REDIS_CONNECTION.set("abc", expected)
    # Read it from the test client
    got = redis_client.get("abc").decode()
    assert expected == got
    redis_client.flushall()


@pytest.mark.asyncio
async def test_no_mss_connected():
    """Raises connection errors only when MSS is unavailable and is not standalone"""
    remove_modules(["os", "app", "settings"])

    os.environ["EXECUTOR_TYPE"] = "quantify"
    os.environ["BACKEND_SETTINGS"] = TEST_BACKEND_SETTINGS_FILE
    os.environ["IS_STANDALONE"] = "False"

    from sqlmodel import SQLModel

    SQLModel.metadata.clear()

    with pytest.raises(requests.exceptions.ConnectionError):
        from app.api import app

        # just to run the startup events
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.asyncio
async def test_calibration_seed_required_for_simulator():
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
async def test_calibration_seed_broken_for_simulator():
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
async def test_quantify_metadata_is_broken():
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
async def test_quantify_config_is_broken():
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
