from .utils.env import (
    TEST_BACKEND_SETTINGS_FILE,
    TEST_BOOKING_DB_URL,
    TEST_DEFAULT_PREFIX,
    TEST_DEFAULT_PREFIX_SIM_1Q,
    TEST_DEFAULT_PREFIX_SIM_2Q,
    TEST_LOGFILE_DOWNLOAD_POOL_DIRNAME,
    TEST_MSS_MACHINE_ROOT_URL,
    TEST_QISKIT_1Q_SEED_FILE,
    TEST_QISKIT_2Q_SEED_FILE,
    TEST_QUANTIFY_SEED_FILE,
    TEST_RQ_REDIS_URL,
    TEST_SIMQ1_BACKEND_SETTINGS_FILE,
    TEST_SIMQ2_BACKEND_SETTINGS_FILE,
    TEST_STORAGE_PREFIX_DIRNAME,
    TEST_STORAGE_ROOT,
    setup_test_env,
)

# set up the environment before any other import
setup_test_env()


import importlib
import logging
import os
import shutil
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    NotRequired,
    Optional,
    TypedDict,
)

import numpy as np
import pytest
import redis
from fastapi.testclient import TestClient
from pytest_lazy_fixtures import lf as lazy_fixture
from redis.client import Redis
from rq import SimpleWorker
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from ..libs.device_parameters import DeviceCalibration
from ..libs.queues.dtos import Job
from ..services.scheduler.queues import QueuePool
from .utils.analysis import MockLinearDiscriminantAnalysis
from .utils.api import create_invalid_mss_headers
from .utils.executors import MockQiskitDynamicsExecutor, MockQuantifyExecutor
from .utils.fixtures import load_fixture
from .utils.http import MockHttpResponse, MockHttpSession
from .utils.rq import get_rq_pool_worker

_redis_connection = redis.Redis.from_url(TEST_RQ_REDIS_URL)

MOCK_NOW = "2023-11-27T12:46:48.851656+00:00"
TEST_APP_TOKEN_STRING = "eecbf107ad103f70187923f49c1a1141219da95f1ab3906f"

FASTAPI_CLIENTS = [
    lazy_fixture("quantify_rest_client"),
    lazy_fixture("qiskit_1q_rest_client"),
    lazy_fixture("qiskit_2q_rest_client"),
]

CLIENTS = [
    (lazy_fixture("quantify_rest_client"), lazy_fixture("redis_client")),
    (
        lazy_fixture("qiskit_1q_rest_client"),
        lazy_fixture("redis_client"),
    ),
    (
        lazy_fixture("qiskit_2q_rest_client"),
        lazy_fixture("redis_client"),
    ),
]

CLIENT_AND_RQ_WORKER_TUPLES = [
    (
        lazy_fixture("quantify_rest_client"),
        lazy_fixture("redis_client"),
        lazy_fixture("rq_worker"),
    ),
    (
        lazy_fixture("qiskit_1q_rest_client"),
        lazy_fixture("redis_client"),
        lazy_fixture("rq_worker_for_simulator_1q"),
    ),
    (
        lazy_fixture("qiskit_2q_rest_client"),
        lazy_fixture("redis_client"),
        lazy_fixture("rq_worker_for_simulator_2q"),
    ),
]

_mock_linear_discriminant_analysis = MockLinearDiscriminantAnalysis(
    result={
        "intercept_": np.array([0.70658014]),
        "coef_": np.array([[-24.30078533, 22.61843697]]),
    }
)

_mock_linear_discriminant_analysis_sim2q = MockLinearDiscriminantAnalysis(
    result={
        "intercept_": np.array([-0.3046965737769689, -0.38509625914248247]),
        "coef_": np.array(
            [
                [-33.678566762457024, 20.606250322051658],
                [-37.36457774060694, 22.99757458442274],
            ]
        ),
    }
)

USERS: List[Dict[str, Any]] = load_fixture("users.json")
VALID_BOOKINGS: List["_BasicBookingInfo"] = load_fixture("valid_bookings.json")
INVALID_BOOKINGS: List["_BasicBookingInfo"] = load_fixture("invalid_bookings.json")
JOBS: List[Dict[str, Any]] = load_fixture("job_list.json")
PAGINATION: List["_PaginationInfo"] = load_fixture("pagination.json")

JOBS_HASH_NAME = f"{Job.__module__}.{Job.__qualname__}".lower()

VALID_CREATE_BOOKINGS_PARAMS = [
    (USERS[0], booking, client)
    for booking in VALID_BOOKINGS
    for client in FASTAPI_CLIENTS
]
INVALID_CREATE_BOOKINGS_PARAMS = [
    (USERS[0], booking, client)
    for booking in INVALID_BOOKINGS
    for client in FASTAPI_CLIENTS
]


@pytest.fixture
def redis_client() -> Redis:
    """A mock redis client"""
    yield _redis_connection
    _redis_connection.flushall()


@pytest.fixture
def rq_worker(redis_client) -> SimpleWorker:
    """Get the rq worker for running async tasks asynchronously for the default backend"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX, connection=redis_client, is_async=True
    )
    yield get_rq_pool_worker(queue_pool)


@pytest.fixture
def rq_worker_for_simulator_1q(redis_client) -> SimpleWorker:
    """Get the rq worker for running async tasks asynchronously for the 1 qubit simulator"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX_SIM_1Q, connection=redis_client, is_async=True
    )
    yield get_rq_pool_worker(queue_pool)


@pytest.fixture
def rq_worker_for_simulator_2q(redis_client) -> SimpleWorker:
    """Get the rq worker for running async tasks asynchronously for the 2 qubit simulator"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX_SIM_2Q, connection=redis_client, is_async=True
    )
    yield get_rq_pool_worker(queue_pool)


def mock_post_requests(url: str, **kwargs):
    """Mock POST requests for testing"""
    if url == f"{TEST_MSS_MACHINE_ROOT_URL}/timelog":
        return MockHttpResponse(status_code=200)


def mock_mss_put_requests(url: str, **kwargs):
    """Mock PUT requests sent to MSS for testing"""
    payload = kwargs.get("json", {})
    is_jobs_update_url = url.startswith(f"{TEST_MSS_MACHINE_ROOT_URL}/jobs")

    if is_jobs_update_url and "timestamps" in payload:
        return MockHttpResponse(status_code=200)
    if is_jobs_update_url and "result" in payload:
        return MockHttpResponse(status_code=200)
    if url.startswith(f"{TEST_MSS_MACHINE_ROOT_URL}/devices"):
        return MockHttpResponse(status_code=200)

    return MockHttpResponse(status_code=405)


def mock_mss_post_requests(url: str, **kwargs):
    """Mock POST requests sent to MSS for testing"""
    payload = kwargs.get("json", [])

    if url.startswith(f"{TEST_MSS_MACHINE_ROOT_URL}/calibrations"):
        try:
            _parsed_payload = [DeviceCalibration(**props) for props in payload]
            return MockHttpResponse(status_code=200)
        except Exception as exp:
            logging.error(exp)
            return MockHttpResponse(status_code=400)

    return MockHttpResponse(status_code=405)


@pytest.fixture
def quantify_rest_client(mocker, redis_client) -> TestClient:
    """A test client for fast api when rq is running asynchronously"""
    _patch_async_client(mocker)
    os.environ["EXECUTOR_TYPE"] = "quantify"
    os.environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX
    os.environ["BACKEND_SETTINGS"] = TEST_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = TEST_QUANTIFY_SEED_FILE

    mocker.patch(
        "app.services.scheduler.utils.QuantifyExecutor", new=MockQuantifyExecutor
    )

    import app
    import settings

    importlib.reload(settings)
    importlib.reload(app)
    from app import api

    yield TestClient(api.app)
    _clear_test_db(TEST_BOOKING_DB_URL)


@pytest.fixture
def qiskit_1q_rest_client(mocker) -> TestClient:
    """A test client for fast api when rq is running asynchronously"""
    _patch_async_client(mocker)
    os.environ["EXECUTOR_TYPE"] = "qiskit_pulse_1q"
    os.environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX_SIM_1Q
    os.environ["BACKEND_SETTINGS"] = TEST_SIMQ1_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = TEST_QISKIT_1Q_SEED_FILE

    import app
    import settings

    importlib.reload(settings)
    importlib.reload(app)
    from app import api

    yield TestClient(api.app)
    _clear_test_db(TEST_BOOKING_DB_URL)
    _redis_connection.flushall()


@pytest.fixture
def qiskit_2q_rest_client(mocker) -> TestClient:
    """A test client for fast api when rq is running asynchronously"""
    _patch_async_client_sim2q(mocker)
    os.environ["EXECUTOR_TYPE"] = "qiskit_pulse_2q"
    os.environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX_SIM_2Q
    os.environ["BACKEND_SETTINGS"] = TEST_SIMQ2_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = TEST_QISKIT_2Q_SEED_FILE

    import app
    import settings

    importlib.reload(settings)
    importlib.reload(app)
    from app import api

    yield TestClient(api.app)
    _clear_test_db(TEST_BOOKING_DB_URL)
    _redis_connection.flushall()


@pytest.fixture
def jobs_folder() -> Path:
    """A temporary folder for the client where jobs can be saved"""
    folder_path = Path("./tmp/jobs")
    folder_path.mkdir(parents=True, exist_ok=True)

    yield folder_path
    shutil.rmtree(folder_path, ignore_errors=True)


@pytest.fixture
def logfile_download_folder() -> Path:
    """A temporary folder for the server where logfiles can be downloaded from"""
    folder_path = (
        Path(TEST_STORAGE_ROOT)
        / TEST_STORAGE_PREFIX_DIRNAME
        / TEST_LOGFILE_DOWNLOAD_POOL_DIRNAME
    )
    folder_path.mkdir(parents=True, exist_ok=True)

    yield folder_path
    shutil.rmtree(folder_path, ignore_errors=True)


@pytest.fixture
def storage_root():
    """root where files are stored temporarily"""
    path = Path(TEST_STORAGE_ROOT)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def app_token_header() -> Dict[str, str]:
    """the authorization header with the app token"""

    yield {"Authorization": f"Bearer {TEST_APP_TOKEN_STRING}"}


@pytest.fixture
def invalid_mss_headers() -> Dict[str, str]:
    """The headers from MSS that are wrongly formatted or something"""
    yield create_invalid_mss_headers()


def _patch_async_client(mocker):
    """Patches the async client"""
    mss_client = MockHttpSession(
        put=mock_mss_put_requests,
        post=mock_mss_post_requests,
    )

    mocker.patch(
        "app.services.scheduler.utils.QuantifyExecutor", new=MockQuantifyExecutor
    )
    mocker.patch(
        "app.services.scheduler.utils.QiskitDynamicsExecutor",
        new=MockQiskitDynamicsExecutor,
    )
    mocker.patch("requests.post", side_effect=mock_post_requests)
    mocker.patch("requests.Session", return_value=mss_client)
    mocker.patch(
        "app.libs.quantum_executor.qiskit.backends.one_qubit.LinearDiscriminantAnalysis",
        return_value=_mock_linear_discriminant_analysis,
    )
    os.environ["BLACKLISTED"] = ""


def _patch_async_client_sim2q(mocker):
    """Patches the async client"""
    mss_client = MockHttpSession(
        put=mock_mss_put_requests,
        post=mock_mss_post_requests,
    )

    mocker.patch(
        "app.services.scheduler.utils.QuantifyExecutor", new=MockQuantifyExecutor
    )
    mocker.patch(
        "app.services.scheduler.utils.QiskitDynamicsExecutor",
        new=MockQiskitDynamicsExecutor,
    )
    mocker.patch("requests.post", side_effect=mock_post_requests)
    mocker.patch("requests.Session", return_value=mss_client)
    mocker.patch(
        "app.libs.quantum_executor.qiskit.backends.two_qubit.LinearDiscriminantAnalysis",
        return_value=_mock_linear_discriminant_analysis_sim2q,
    )
    os.environ["BLACKLISTED"] = ""


def _clear_test_db(url: str = TEST_BOOKING_DB_URL):
    """Clears the test database

    Args:
        url: the database URL for the database
    """
    db = create_engine(url)
    SQLModel.metadata.drop_all(db)


class _BasicBookingInfo(TypedDict):
    """The simplified basic booking info"""

    starts_in: float
    duration: float
    error_message: NotRequired[str]


class _PaginationInfo(TypedDict):
    """The pagination info"""

    skip: int
    limit: Optional[float]
