# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Fixtures of the REST API's test clients, one per executor"""
import importlib
import importlib.util
from typing import (
    Any,
    Generator,
)

import pytest
import toml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from app.tests.utils.analysis import MOCK_LDA_1Q, MOCK_LDA_2Q
from app.tests.utils.api import get_test_client
from app.tests.utils.env import (
    TEST_BACKEND_SETTINGS_FILE,
    TEST_BOOKING_DB_URL,
    TEST_DEFAULT_PREFIX,
    TEST_DEFAULT_PREFIX_SIM_1Q,
    TEST_DEFAULT_PREFIX_SIM_2Q,
    TEST_QISKIT_1Q_SEED_FILE,
    TEST_QISKIT_2Q_SEED_FILE,
    TEST_QUANTIFY_SEED_FILE,
    TEST_SIMQ1_BACKEND_SETTINGS_FILE,
    TEST_SIMQ2_BACKEND_SETTINGS_FILE,
)


@pytest.fixture
def quantify_seed_file(tmp_path) -> Generator[str, Any, None]:
    """Returns a path to a temporary copy of the dummy quantify calibration seed file"""
    contents = {}
    with open(TEST_QUANTIFY_SEED_FILE, "r") as file:
        contents = toml.load(file)

    new_seed_file = tmp_path / "dummy_calibration.seed.toml"
    with open(new_seed_file, "w") as file:
        toml.dump(contents, file)

    yield str(new_seed_file)

    new_seed_file.unlink(missing_ok=True)


@pytest.fixture
def quantify_rest_client(
    mocker, mock_mss, redis_client, quantify_seed_file, monkeypatch
) -> Generator[TestClient, Any, None]:
    """A test client for fast api when rq is running asynchronously"""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "quantify")
    monkeypatch.setenv("BCC_DEFAULT_PREFIX", TEST_DEFAULT_PREFIX)
    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", quantify_seed_file)

    from app.tests.utils.executors.quantify import MockQuantifyExecutor
    from app.tests.utils.tuner import mock_run_node

    mocker.patch(
        "app.libs.quantum_executor.quantify.executor.QuantifyExecutor",
        new=MockQuantifyExecutor,
    )
    mocker.patch(
        "app.libs.quantum_executor.quantify.utils.calibration.run_node",
        new=mock_run_node,
    )

    import app
    import settings
    from app.services.scheduler.utils import clear_quantum_executor

    clear_quantum_executor(ignore_errors=True)

    importlib.reload(settings)
    importlib.reload(app)
    from app import api

    yield get_test_client(api.app)
    _clear_test_db(TEST_BOOKING_DB_URL)
    clear_quantum_executor(ignore_errors=True)


@pytest.fixture
def qiskit_1q_rest_client(
    request, mocker, mock_mss, redis_client, monkeypatch
) -> Generator[TestClient, Any, None]:
    """A test client for fast api when rq is running asynchronously"""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "qiskit_pulse_1q")
    monkeypatch.setenv("BCC_DEFAULT_PREFIX", TEST_DEFAULT_PREFIX_SIM_1Q)
    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_SIMQ1_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", TEST_QISKIT_1Q_SEED_FILE)

    from app.tests.utils.executors.qiskit import (
        MockQiskitDynamicsExecutor,
        StubQiskitDynamicsExecutor,
    )

    # only a test that checks the measured counts pays for simulating
    executor_class = (
        MockQiskitDynamicsExecutor
        if request.node.get_closest_marker("simulated")
        else StubQiskitDynamicsExecutor
    )

    mocker.patch(
        "app.libs.quantum_executor.qiskit.executor.QiskitDynamicsExecutor",
        new=executor_class,
    )
    mocker.patch(
        "app.libs.quantum_executor.qiskit.backends.one_qubit.LinearDiscriminantAnalysis",
        return_value=MOCK_LDA_1Q,
    )

    import app
    import settings
    from app.services.scheduler.utils import clear_quantum_executor

    clear_quantum_executor(ignore_errors=True)

    importlib.reload(settings)
    importlib.reload(app)
    from app import api

    yield get_test_client(api.app)
    _clear_test_db(TEST_BOOKING_DB_URL)
    clear_quantum_executor(ignore_errors=True)


@pytest.fixture
def qiskit_2q_rest_client(
    request, mocker, mock_mss, redis_client, monkeypatch
) -> Generator[TestClient, Any, None]:
    """A test client for fast api when rq is running asynchronously"""
    monkeypatch.setenv("BCC_EXECUTOR_TYPE", "qiskit_pulse_2q")
    monkeypatch.setenv("BCC_DEFAULT_PREFIX", TEST_DEFAULT_PREFIX_SIM_2Q)
    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_SIMQ2_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", TEST_QISKIT_2Q_SEED_FILE)

    from app.tests.utils.executors.qiskit import (
        MockQiskitDynamicsExecutor,
        StubQiskitDynamicsExecutor,
    )

    # only a test that checks the measured counts pays for simulating
    executor_class = (
        MockQiskitDynamicsExecutor
        if request.node.get_closest_marker("simulated")
        else StubQiskitDynamicsExecutor
    )

    mocker.patch(
        "app.libs.quantum_executor.qiskit.executor.QiskitDynamicsExecutor",
        new=executor_class,
    )
    mocker.patch(
        "app.libs.quantum_executor.qiskit.backends.two_qubit.LinearDiscriminantAnalysis",
        return_value=MOCK_LDA_2Q,
    )

    import app
    import settings
    from app.services.scheduler.utils import clear_quantum_executor

    clear_quantum_executor(ignore_errors=True)

    importlib.reload(settings)
    importlib.reload(app)
    from app import api

    yield get_test_client(api.app)
    _clear_test_db(TEST_BOOKING_DB_URL)
    clear_quantum_executor(ignore_errors=True)


def _clear_test_db(url: str = TEST_BOOKING_DB_URL):
    """Clears the test database

    Args:
        url: the database URL for the database
    """
    db = create_engine(url)
    SQLModel.metadata.drop_all(db)
