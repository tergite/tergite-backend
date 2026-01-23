import asyncio

import websockets
from starlette.datastructures import URL

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
import sys
from pathlib import Path
from typing import (
    Any,
    Dict,
    Generator,
    List,
    NotRequired,
    Optional,
    Tuple,
    TypedDict,
)

import numpy as np
import pytest
import redis
from fastapi.testclient import TestClient
from pytest_lazy_fixtures import lf as lazy_fixture
from qblox_instruments import SpiRack
from redis.client import Redis
from rq import SimpleWorker
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from ..libs.quantum_executor.quantify.spi_dac import SpiDAC
from ..libs.queues.dtos import Job
from ..services.scheduler.queues import QueuePool
from .utils.analysis import MockLinearDiscriminantAnalysis
from .utils.executors import MockQiskitDynamicsExecutor, MockQuantifyExecutor
from .utils.fixtures import get_fixture_path, load_fixture
from .utils.mocks import make_attr_verbose
from .utils.mss import mock_mss_websocket_handler
from .utils.rq import get_rq_pool_worker

_redis_connection = redis.Redis.from_url(TEST_RQ_REDIS_URL)

MOCK_NOW = "2023-11-27T12:46:48.851656+00:00"

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
_PERMITTED_EVENTS_TO_MSS = ("initialized", "recalibrated", "job_updated")

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
VALID_BOOKINGS: List["BasicBookingInfo"] = load_fixture("valid_bookings.json")
INVALID_BOOKINGS: List["BasicBookingInfo"] = load_fixture("invalid_bookings.json")
JOBS: List[Dict[str, Any]] = load_fixture("job_list.json")
PAGINATION: List["PaginationInfo"] = load_fixture("pagination.json")
SPI_DUMMY_METADATA_FILE = get_fixture_path("spi_dummy_quantify-metadata.yml")
TEST_SPI_LOGGER_NAME = "test.spi_dac.verbose"

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
def redis_client() -> Generator[Redis, Any, None]:
    """A mock redis client"""
    yield _redis_connection
    _redis_connection.flushall()


@pytest.fixture
def rq_worker(redis_client) -> Generator[SimpleWorker, Any, None]:
    """Get the rq worker for running async tasks asynchronously for the default backend"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX, connection=redis_client, is_async=True
    )
    yield get_rq_pool_worker(queue_pool)


@pytest.fixture
def rq_worker_for_simulator_1q(redis_client) -> Generator[SimpleWorker, Any, None]:
    """Get the rq worker for running async tasks asynchronously for the 1 qubit simulator"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX_SIM_1Q, connection=redis_client, is_async=True
    )
    yield get_rq_pool_worker(queue_pool)


@pytest.fixture
def rq_worker_for_simulator_2q(redis_client) -> Generator[SimpleWorker, Any, None]:
    """Get the rq worker for running async tasks asynchronously for the 2 qubit simulator"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX_SIM_2Q, connection=redis_client, is_async=True
    )
    yield get_rq_pool_worker(queue_pool)


@pytest.fixture
def quantify_rest_client(
    mocker, redis_client, mock_mss_websockets
) -> Generator[TestClient, Any, None]:
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
async def mock_mss_websockets():
    """A mock websocket for connecting to MSS"""
    mss_url = URL(TEST_MSS_MACHINE_ROOT_URL)
    async with websockets.serve(
        mock_mss_websocket_handler, mss_url.hostname, mss_url.port
    ) as server:
        # Get the actual port assigned by the OS
        host, port, *rest = server.sockets[0].getsockname()
        yield f"ws://{host}:{port}"


@pytest.fixture
def qiskit_1q_rest_client(
    mocker, mock_mss_websockets
) -> Generator[TestClient, Any, None]:
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
def qiskit_2q_rest_client(
    mocker, mock_mss_websockets
) -> Generator[TestClient, Any, None]:
    """A test client for fast api when rq is running asynchronously"""
    _patch_async_client(mocker)
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


@pytest.fixture(scope="session")
def jobs_folder() -> Generator[Path, Any, None]:
    """A temporary folder for the client where jobs can be saved"""
    folder_path = Path("./tmp/jobs")
    folder_path.mkdir(parents=True, exist_ok=True)

    yield folder_path
    shutil.rmtree(folder_path, ignore_errors=True)


@pytest.fixture(scope="session")
def logfile_download_folder() -> Generator[Path, Any, None]:
    """A temporary folder for the server where logfiles can be downloaded from"""
    folder_path = (
        Path(TEST_STORAGE_ROOT)
        / TEST_STORAGE_PREFIX_DIRNAME
        / TEST_LOGFILE_DOWNLOAD_POOL_DIRNAME
    )
    folder_path.mkdir(parents=True, exist_ok=True)

    yield folder_path
    shutil.rmtree(folder_path, ignore_errors=True)


@pytest.fixture(scope="session")
def storage_root():
    """root where files are stored temporarily"""
    path = Path(TEST_STORAGE_ROOT)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def spi_dac_dummy(redis_client) -> Generator[SpiDAC, Any, None]:
    """
    Construct SpiDAC bound to the dummy SPI-Rack.
    """
    name = os.environ.get("DEFAULT_PREFIX", "quantify")
    metadata_path = SPI_DUMMY_METADATA_FILE
    couplers = ["u0"]

    if SpiDAC.exist(name):
        SpiRack.find_instrument(name).close()

    sd = SpiDAC(
        couplers=couplers,
        metadata_path=metadata_path,
        connection=redis_client,
        name=name,
    )
    yield sd

    try:
        sd.close_spi_rack()
    except:
        pass


@pytest.fixture
def verbose_spi_dac_dummy(redis_client, mocker) -> Generator[SpiDAC, Any, None]:
    """
    Construct SpiDAC bound to the dummy SPI-Rack.
    """
    name = os.environ.get("DEFAULT_PREFIX", "quantify")
    metadata_path = SPI_DUMMY_METADATA_FILE
    couplers = ["u0"]

    testlog = logging.getLogger(TEST_SPI_LOGGER_NAME)
    testlog.setLevel(logging.DEBUG)

    for fn in [
        "__init__",
        "create_spi_dac",
        "set_dacs_zero",
        "exist",
        "set_parking_currents",
        "set_dac_current",
        "ramp_current_serially",
        "ramp_current_simultaneusly",
        "print_currents",
        "close_spi_rack",
    ]:
        make_attr_verbose(SpiDAC, mock_fixture=mocker, logger=testlog, attr_name=fn)

    if SpiDAC.exist(name):
        SpiRack.find_instrument(name).close()

    sd = SpiDAC(
        couplers=couplers,
        metadata_path=metadata_path,
        connection=redis_client,
        name=name,
    )
    yield sd

    try:
        sd.close_spi_rack()
    except:
        pass


@pytest.fixture(autouse=True, scope="session")
def _configure_logging_for_tests():
    """Configure logging for tests"""
    is_debug = os.getenv("DEBUG", "").strip().lower() == "true"
    if not is_debug:
        yield
        return

    if is_debug:
        root = logging.getLogger()
        # Remove any preconfigured handlers (libraries may have added them)
        for h in root.handlers[:]:
            root.removeHandler(h)
        # Use stderr (or sys.__stdout__) which we won't close
        h = logging.StreamHandler(sys.__stderr__)
        fmt = logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
        h.setFormatter(fmt)
        root.addHandler(h)
        root.setLevel(
            getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
        )
        yield
        # No need to close sys.__stderr__; just flush
        for h in root.handlers[:]:
            try:
                h.flush()
            except:
                pass
    else:
        # FIXME: This never runs
        # silence rq logs
        logging.getLogger("rq").setLevel(logging.WARNING)
        logging.getLogger("rq.worker").setLevel(logging.WARNING)
        logging.getLogger("rq.queue").setLevel(logging.WARNING)
        yield


def _patch_async_client(mocker, *extra_patches: Tuple[str, Dict[str, Any]]):
    """Patches the async client

    Args:
        mocker: the pytest mocker object
        extra_patches: extra patches to patch with the mocker object
    """

    mocker.patch(
        "app.services.scheduler.utils.QuantifyExecutor", new=MockQuantifyExecutor
    )
    mocker.patch(
        "app.services.scheduler.utils.QiskitDynamicsExecutor",
        new=MockQiskitDynamicsExecutor,
    )

    mocker.patch(
        "app.libs.quantum_executor.qiskit.backends.one_qubit.LinearDiscriminantAnalysis",
        return_value=_mock_linear_discriminant_analysis,
    )
    mocker.patch(
        "app.libs.quantum_executor.qiskit.backends.two_qubit.LinearDiscriminantAnalysis",
        return_value=_mock_linear_discriminant_analysis_sim2q,
    )
    os.environ["BLACKLISTED"] = ""

    for url, kwargs in extra_patches:
        mocker.patch(url, **kwargs)


def _clear_test_db(url: str = TEST_BOOKING_DB_URL):
    """Clears the test database

    Args:
        url: the database URL for the database
    """
    db = create_engine(url)
    SQLModel.metadata.drop_all(db)


class BasicBookingInfo(TypedDict):
    """The simplified basic booking info"""

    starts_in: float
    duration: float
    error_message: NotRequired[str]


class PaginationInfo(TypedDict):
    """The pagination info"""

    skip: int
    limit: Optional[float]
