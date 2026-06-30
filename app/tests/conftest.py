import toml

from .utils.env import (
    TEST_BACKEND_SETTINGS_FILE,
    TEST_BOOKING_DB_URL,
    TEST_DEFAULT_PREFIX,
    TEST_DEFAULT_PREFIX_SIM_1Q,
    TEST_DEFAULT_PREFIX_SIM_2Q,
    TEST_LOGFILE_DOWNLOAD_POOL_DIRNAME,
    TEST_MAX_EXECUTION_TIME,
    TEST_MAX_GENERAL_QUEUE_TIME,
    TEST_MAX_POSTPROCESSING_TIME,
    TEST_MAX_PREPROCESSING_TIME,
    TEST_MAX_RECALIBRATION_QUEUE_TIME,
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
from contextlib import suppress
from pathlib import Path
from typing import (
    Any,
    Dict,
    Generator,
    List,
    Literal,
    NotRequired,
    Optional,
    Tuple,
    TypedDict,
    Union,
)

import numpy as np
import pytest
import redis
from fastapi.testclient import TestClient
from pytest_lazy_fixtures import lf as lazy_fixture
from pytest_mock import MockerFixture
from redis.client import Redis
from rq import SimpleWorker, Worker
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from ..libs.queues.dtos import Job
from ..services.scheduler.queues import QueuePool
from .utils.analysis import MockLinearDiscriminantAnalysis
from .utils.fixtures import load_fixture
from .utils.mss import mock_sync_connect
from .utils.rq import get_rq_pool_worker

HAS_QISKIT_DYNAMICS = False
HAS_QUANTIFY = False

with suppress(ImportError):
    import qiskit_dynamics

    HAS_QISKIT_DYNAMICS = True

with suppress(ImportError):
    import quantify_scheduler

    HAS_QUANTIFY = True

_redis_connection = redis.Redis.from_url(TEST_RQ_REDIS_URL)

MOCK_NOW = "2023-11-27T12:46:48.851656+00:00"

FASTAPI_CLIENTS = []
CLIENTS = []
CLIENT_AND_RQ_WORKER_TUPLES = []

if HAS_QUANTIFY:
    FASTAPI_CLIENTS += [
        lazy_fixture("quantify_rest_client"),
    ]
    CLIENTS += [
        (lazy_fixture("quantify_rest_client"), lazy_fixture("redis_client")),
    ]
    CLIENT_AND_RQ_WORKER_TUPLES += [
        (
            lazy_fixture("quantify_rest_client"),
            lazy_fixture("redis_client"),
            lazy_fixture("rq_worker"),
        ),
    ]

if HAS_QISKIT_DYNAMICS:
    FASTAPI_CLIENTS += [
        lazy_fixture("qiskit_1q_rest_client"),
        lazy_fixture("qiskit_2q_rest_client"),
    ]
    CLIENTS += [
        (
            lazy_fixture("qiskit_1q_rest_client"),
            lazy_fixture("redis_client"),
        ),
        (
            lazy_fixture("qiskit_2q_rest_client"),
            lazy_fixture("redis_client"),
        ),
    ]
    CLIENT_AND_RQ_WORKER_TUPLES += [
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
VALID_BOOKINGS: List["BasicBookingInfo"] = load_fixture("valid_bookings.json")
INVALID_BOOKINGS: List["BasicBookingInfo"] = load_fixture("invalid_bookings.json")
JOBS: List[Dict[str, Any]] = load_fixture("job_list.json")
PAGINATION: List["PaginationInfo"] = load_fixture("pagination.json")
RECALIBRATION_MOCKS: Dict[Literal["qubit", "coupler"], Dict[str, Any]] = load_fixture(
    "recalibration-mocks.json"
)

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
def rq_worker(redis_client) -> Generator[Union[Worker, SimpleWorker], Any, None]:
    """Get the rq worker for running async tasks asynchronously for the default backend"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX,
        connection=redis_client,
        is_async=True,
        execution_timeout=TEST_MAX_EXECUTION_TIME,
        preprocessing_timeout=TEST_MAX_PREPROCESSING_TIME,
        postprocessing_timeout=TEST_MAX_POSTPROCESSING_TIME,
        general_queue_timeout=TEST_MAX_GENERAL_QUEUE_TIME,
        recalibration_timeout=TEST_MAX_RECALIBRATION_QUEUE_TIME,
    )
    yield get_rq_pool_worker(queue_pool)


@pytest.fixture
def rq_worker_for_simulator_1q(
    redis_client,
) -> Generator[Union[Worker, SimpleWorker], Any, None]:
    """Get the rq worker for running async tasks asynchronously for the 1 qubit simulator"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX_SIM_1Q,
        connection=redis_client,
        is_async=True,
        execution_timeout=TEST_MAX_EXECUTION_TIME,
        preprocessing_timeout=TEST_MAX_PREPROCESSING_TIME,
        postprocessing_timeout=TEST_MAX_POSTPROCESSING_TIME,
        general_queue_timeout=TEST_MAX_GENERAL_QUEUE_TIME,
        recalibration_timeout=TEST_MAX_RECALIBRATION_QUEUE_TIME,
    )
    yield get_rq_pool_worker(queue_pool)


@pytest.fixture
def rq_worker_for_simulator_2q(
    redis_client,
) -> Generator[Union[Worker, SimpleWorker], Any, None]:
    """Get the rq worker for running async tasks asynchronously for the 2 qubit simulator"""
    queue_pool = QueuePool(
        prefix=TEST_DEFAULT_PREFIX_SIM_2Q,
        connection=redis_client,
        is_async=True,
        execution_timeout=TEST_MAX_EXECUTION_TIME,
        preprocessing_timeout=TEST_MAX_PREPROCESSING_TIME,
        postprocessing_timeout=TEST_MAX_POSTPROCESSING_TIME,
        general_queue_timeout=TEST_MAX_GENERAL_QUEUE_TIME,
        recalibration_timeout=TEST_MAX_RECALIBRATION_QUEUE_TIME,
    )
    yield get_rq_pool_worker(queue_pool)


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
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


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
@pytest.fixture
def quantify_rest_client(
    mocker, redis_client, quantify_seed_file
) -> Generator[TestClient, Any, None]:
    """A test client for fast api when rq is running asynchronously"""
    _patch_async_client(mocker)
    os.environ["EXECUTOR_TYPE"] = "quantify"
    os.environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX
    os.environ["BACKEND_SETTINGS"] = TEST_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = quantify_seed_file

    from .utils.executors.quantify import MockQuantifyExecutor
    from .utils.tuner import mock_run_node

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

    importlib.reload(settings)
    importlib.reload(app)
    from app import api

    yield TestClient(api.app)
    _clear_test_db(TEST_BOOKING_DB_URL)


@pytest.fixture
def patched_mss_websockets(mocker) -> Generator[MockerFixture, Any, None]:
    """Patch the websocket used to connect to MSS"""
    mocker.patch(
        "app.services.external.mss.service.connect", side_effect=mock_sync_connect
    )
    yield mocker


@pytest.mark.skipif(not HAS_QISKIT_DYNAMICS, reason="requires qiskit")
@pytest.fixture
def qiskit_1q_rest_client(mocker) -> Generator[TestClient, Any, None]:
    """A test client for fast api when rq is running asynchronously"""
    os.environ["EXECUTOR_TYPE"] = "qiskit_pulse_1q"
    os.environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX_SIM_1Q
    os.environ["BACKEND_SETTINGS"] = TEST_SIMQ1_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = TEST_QISKIT_1Q_SEED_FILE

    from .utils.executors.qiskit import MockQiskitDynamicsExecutor

    mocker.patch(
        "app.services.external.mss.service.connect", side_effect=mock_sync_connect
    )
    mocker.patch(
        "app.libs.quantum_executor.qiskit.executor.QiskitDynamicsExecutor",
        new=MockQiskitDynamicsExecutor,
    )
    mocker.patch(
        "app.libs.quantum_executor.qiskit.backends.one_qubit.LinearDiscriminantAnalysis",
        return_value=_mock_linear_discriminant_analysis,
    )

    import app
    import settings

    importlib.reload(settings)
    importlib.reload(app)
    from app import api

    yield TestClient(api.app)
    _clear_test_db(TEST_BOOKING_DB_URL)
    _redis_connection.flushall()


@pytest.mark.skipif(not HAS_QISKIT_DYNAMICS, reason="requires qiskit")
@pytest.fixture
def qiskit_2q_rest_client(mocker) -> Generator[TestClient, Any, None]:
    """A test client for fast api when rq is running asynchronously"""
    _patch_async_client(mocker)
    os.environ["EXECUTOR_TYPE"] = "qiskit_pulse_2q"
    os.environ["DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX_SIM_2Q
    os.environ["BACKEND_SETTINGS"] = TEST_SIMQ2_BACKEND_SETTINGS_FILE
    os.environ["CALIBRATION_SEED"] = TEST_QISKIT_2Q_SEED_FILE

    from .utils.executors.qiskit import MockQiskitDynamicsExecutor

    mocker.patch(
        "app.libs.quantum_executor.qiskit.executor.QiskitDynamicsExecutor",
        new=MockQiskitDynamicsExecutor,
    )
    mocker.patch(
        "app.libs.quantum_executor.qiskit.backends.two_qubit.LinearDiscriminantAnalysis",
        return_value=_mock_linear_discriminant_analysis_sim2q,
    )

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


@pytest.fixture(autouse=True)
def _clear_connection_caches():
    """Reset all module-level connection caches before every test."""
    from app.services.external.mss.service import disconnect_mss_client
    from app.services.scheduler.store import clear_jobs_stores_registry
    from app.services.scheduler.utils import clear_quantum_executor
    from app.utils.redis import clear_redis_connections

    clear_redis_connections(ignore_errors=True)
    clear_jobs_stores_registry()
    disconnect_mss_client(ignore_errors=True)
    clear_quantum_executor(ignore_errors=True)
    yield
    clear_redis_connections(ignore_errors=True)
    clear_jobs_stores_registry()
    disconnect_mss_client(ignore_errors=True)
    clear_quantum_executor(ignore_errors=True)


@pytest.fixture
def redis_from_url_spy(mocker: MockerFixture):
    """Spy on redis.Redis.from_url to count new Redis connections opened during a test."""
    import redis

    yield mocker.spy(redis.Redis, "from_url")


@pytest.fixture
def mss_connect_spy(mocker: MockerFixture):
    """Spy on websockets.sync.client.connect (as imported in mss.service) to count
    new MSS websocket connections opened during a test.
    """
    import app.services.external.mss.service as _svc

    yield mocker.spy(_svc, "connect")


@pytest.fixture
def sql_engine_spy(mocker: MockerFixture):
    """Spy on create_engine (as imported in app.utils.sql_db) to count new
    SQLAlchemy engines created during a test.
    """
    from app.utils import sql_db

    yield mocker.spy(sql_db, "create_engine")


@pytest.fixture
def connection_call_log(tmp_path):
    """Patches all connection factories to append JSON call records to a file.

    Works across fork boundaries — PreloadedTestWorker forks a child for each
    job, and the child inherits the patched functions, so every connection-open
    call from any process is recorded.  Each line written to the file is:

        {"resource": "redis"|"mss"|"sql_engine", "pid": <int>, "url": <str>}

    Typical assertion pattern::

        def test_connections_reused(connection_call_log, rq_worker, ...):
            # ... enqueue and run jobs ...
            records = read_connection_log(connection_call_log)
            redis_calls = [r for r in records if r["resource"] == "redis"]
            # created once in the parent (preload), never again in children
            assert len(redis_calls) == 1
            assert redis_calls[0]["pid"] == os.getpid()

    Note: set up this fixture *before* creating the rq_worker so the patches
    are already in place when _preload() runs.
    """
    import json

    import app.services.external.mss.service as _mss_svc
    from app.utils import sql_db

    log_path = tmp_path / "connection_calls.jsonl"

    _orig_from_url = redis.Redis.from_url
    _orig_connect = _mss_svc.connect
    _orig_create_engine = sql_db.create_engine

    def _track_from_url(url, **kwargs):
        with open(log_path, "a") as f:
            f.write(
                json.dumps({"resource": "redis", "pid": os.getpid(), "url": str(url)})
                + "\n"
            )
        return _orig_from_url(url, **kwargs)

    def _track_connect(uri, **kwargs):
        with open(log_path, "a") as f:
            f.write(
                json.dumps({"resource": "mss", "pid": os.getpid(), "url": str(uri)})
                + "\n"
            )
        return _orig_connect(uri, **kwargs)

    def _track_create_engine(*args, **kwargs):
        url = str(args[0]) if args else str(kwargs.get("url", ""))
        with open(log_path, "a") as f:
            f.write(
                json.dumps({"resource": "sql_engine", "pid": os.getpid(), "url": url})
                + "\n"
            )
        return _orig_create_engine(*args, **kwargs)

    redis.Redis.from_url = staticmethod(_track_from_url)
    _mss_svc.connect = _track_connect
    sql_db.create_engine = _track_create_engine

    yield str(log_path)

    redis.Redis.from_url = staticmethod(_orig_from_url)
    _mss_svc.connect = _orig_connect
    sql_db.create_engine = _orig_create_engine


def read_connection_log(log_path: str) -> list:
    """Parse the JSONL file written by the connection_call_log fixture.

    Returns a list of dicts, each with keys: resource, pid, url.
    """
    import json

    try:
        with open(log_path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


@pytest.fixture(autouse=True, scope="session")
def _configure_logging_for_tests():
    """Configure logging for tests"""
    is_debug = os.getenv("DEBUG", "").strip().lower() == "true"
    if not is_debug:
        # silence rq logs
        logging.getLogger("rq").setLevel(logging.WARNING)
        logging.getLogger("rq.worker").setLevel(logging.WARNING)
        logging.getLogger("rq.queue").setLevel(logging.WARNING)
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


def _patch_async_client(mocker, *extra_patches: Tuple[str, Dict[str, Any]]):
    """Patches the async client

    Args:
        mocker: the pytest mocker object
        extra_patches: extra patches to patch with the mocker object
    """
    mocker.patch(
        "app.services.external.mss.service.connect", side_effect=mock_sync_connect
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
