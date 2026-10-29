# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2024
# (C) Copyright Chalmers Next Labs AB 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
import atexit
import os
import shutil
import tempfile
from os import environ
from pathlib import Path

from app.tests.utils.fixtures import get_fixture_path

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.resolve()
_TESTS_FOLDER = _PROJECT_ROOT / "app" / "tests"
# set when pytest-xdist runs the tests, e.g. gw3; each worker has its own files and Redis database
_XDIST_WORKER = os.getenv("PYTEST_XDIST_WORKER", "gw0")

# made on import, as the settings read it before any fixture runs; the suite runs in it.
# Removed at exit, not by a fixture, as xdist's controller imports this but runs no fixture
TEST_ROOT = Path(tempfile.mkdtemp(prefix=f"bcc-tests-{_XDIST_WORKER}-"))
atexit.register(shutil.rmtree, TEST_ROOT, ignore_errors=True)
_PATH_TO_TEST_SQL_DB = TEST_ROOT / "booking.db"

TEST_DEFAULT_PREFIX = "system_test"
TEST_STORAGE_ROOT = f"{TEST_ROOT / 'storage'}"

TEST_DEFAULT_PREFIX_SIM_1Q = "qiskit_pulse_1q"
TEST_DEFAULT_PREFIX_SIM_2Q = "qiskit_pulse_2q"

TEST_LOGFILE_DOWNLOAD_POOL_DIRNAME = "logfile_download_pool"
TEST_JOB_UPLOAD_POOL_DIRNAME = "job_upload_pool"
TEST_JOB_PRE_PROC_POOL_DIRNAME = "job_pre_proc_pool"
TEST_STORAGE_PREFIX_DIRNAME = TEST_DEFAULT_PREFIX
TEST_JOB_SUPERVISOR_LOG = "job_supervisor.log"
TEST_EXECUTOR_DATA_DIRNAME = "test_executor_data"

TEST_MSS_PORT = os.getenv("MSS_PORT", "8005")
TEST_MSS_MACHINE_ROOT_URL = f"http://localhost:{TEST_MSS_PORT}"
TEST_BCC_MACHINE_ROOT_URL = "http://localhost:8000"
TEST_BCC_PORT = 8000

TEST_MSS_APP_TOKEN = "some-mss-app-token-for-testing"

TEST_CALIBRATION_NODE_CONFIG_FILE = _PROJECT_ROOT / "node_config.example.toml"
TEST_CALIBRATION_SPI_CONFIG_FILE = get_fixture_path("dummy_spi_config.calib.toml")
TEST_CALIBRATION_DEVICE_CONFIG_FILE = _PROJECT_ROOT / "calib_device_config.example.toml"

TEST_QUANTIFY_CONFIG_FILE = get_fixture_path("dummy-quantify-config.json")
TEST_QUANTIFY_METADATA_FILE = get_fixture_path("dummy-quantify-metadata.yml")
TEST_BROKEN_QUANTIFY_METADATA_FILE = get_fixture_path("broken-quantify-metadata.yml")
TEST_BROKEN_QUANTIFY_CONFIG_FILE = get_fixture_path("broken-quantify-config.json")

TEST_BACKEND_SETTINGS_FILE = get_fixture_path("backend_config.toml")
TEST_SIMQ1_BACKEND_SETTINGS_FILE = get_fixture_path("backend_config.simq1.toml")
TEST_SIMQ2_BACKEND_SETTINGS_FILE = get_fixture_path("backend_config.simq2.toml")

TEST_QUANTIFY_SEED_FILE = get_fixture_path("dummy_quantify.seed.toml")
TEST_QISKIT_1Q_SEED_FILE = get_fixture_path("qiskit_pulse_1q.seed.toml")
TEST_QISKIT_2Q_SEED_FILE = get_fixture_path("qiskit_pulse_2q.seed.toml")
TEST_MSS_PUBLIC_KEY_PATH = get_fixture_path("mss_public_key.pem")
TEST_MSS_PRIVATE_KEY_PATH = get_fixture_path("mss_private_key.pem")

TEST_PRIVATE_KEY_FILE = get_fixture_path("private-bcc-key.pem")
TEST_PUBLIC_KEY_FILE = get_fixture_path("public-bcc-key.pem")

TEST_MAX_TIME_SLOT_LENGTH = 3600
TEST_MIN_TIME_SLOT_LENGTH = 2
TEST_MAX_IDLE_TIME = 1
TEST_MAX_SLOTS_PER_DAY = 2
TEST_IS_ASYNC = "True"
TEST_BOOKING_DB_URL = os.getenv(
    "TEST_BOOKING_DB_URL", f"sqlite:///{_PATH_TO_TEST_SQL_DB}"
)
TEST_JWT_SECRET = "78e79946910251b0db6237d5eb38ece76225cf613da22b79ff4fe76d534c14fc"
TEST_JWT_TTL = 120
TEST_MSS_NONCE_TTL = 200
TEST_BCC_NONCE_TTL = 200
TEST_CORS_ORIGINS = "testclient,localhost,127.0.0.1"

REDIS_PORT = os.getenv("REDIS_PORT", "6378")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_DB = f"{int(os.getenv('REDIS_DB', '2')) + int(_XDIST_WORKER.removeprefix('gw'))}"

TEST_RQ_REDIS_URL = os.getenv(
    "RQ_REDIS_URL", f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
)
TEST_JOBS_REDIS_URL = TEST_RQ_REDIS_URL
TEST_LOG_LEVEL = os.getenv("LOG_LEVEL", "ERROR")
TEST_DEBUG = os.getenv("DEBUG", "False")
TEST_RQ_MAX_QUEUE_WAIT_TIME = int(os.getenv("TEST_RQ_MAX_QUEUE_WAIT_TIME", "120"))

TEST_MAX_PREPROCESSING_TIME = int(os.getenv("MAX_PREPROCESSING_TIME", "300"))
TEST_MAX_EXECUTION_TIME = int(os.getenv("MAX_EXECUTION_TIME", "600"))
TEST_MAX_POSTPROCESSING_TIME = int(os.getenv("MAX_POSTPROCESSING_TIME", "180"))
TEST_MAX_GENERAL_QUEUE_TIME = int(os.getenv("MAX_GENERAL_QUEUE_TIME", "180"))
TEST_MAX_RECALIBRATION_QUEUE_TIME = int(os.getenv("MAX_RECALIBRATION_QUEUE_TIME", "4"))
TEST_DEFAULT_RECALIBRATION_INTERVAL = float(
    os.getenv("DEFAULT_RECALIBRATION_INTERVAL", "10")
)
TEST_MSS_RESPONSE_TIMEOUT = float(os.getenv("MSS_RESPONSE_TIMEOUT", "10"))
TEST_MSS_CONNECTION_TIMEOUT = float(os.getenv("MSS_CONNECTION_TIMEOUT", "2"))
TEST_MSS_CONNECTION_MAX_ATTEMPTS = int(os.getenv("MSS_CONNECTION_MAX_ATTEMPTS", "1"))


def setup_test_env():
    """Sets up the test environment.

    It should be run before any imports
    """
    environ["BCC_APP_SETTINGS"] = "test"
    environ["BCC_DEFAULT_PREFIX"] = TEST_DEFAULT_PREFIX
    environ["BCC_STORAGE_ROOT"] = TEST_STORAGE_ROOT
    environ["BCC_BACKEND_SETTINGS"] = TEST_BACKEND_SETTINGS_FILE
    environ["BCC_CALIBRATION_SEED"] = TEST_QUANTIFY_SEED_FILE

    environ["BCC_QUANTIFY_CONFIG_FILE"] = TEST_QUANTIFY_CONFIG_FILE
    environ["BCC_QUANTIFY_METADATA_FILE"] = TEST_QUANTIFY_METADATA_FILE
    environ["BCC_CALIBRATION_NODE_CONFIG_FILE"] = f"{TEST_CALIBRATION_NODE_CONFIG_FILE}"
    environ["BCC_CALIBRATION_DEVICE_CONFIG_FILE"] = (
        f"{TEST_CALIBRATION_DEVICE_CONFIG_FILE}"
    )
    environ["BCC_CALIBRATION_SPI_CONFIG_FILE"] = f"{TEST_CALIBRATION_SPI_CONFIG_FILE}"

    environ["BCC_LOGFILE_DOWNLOAD_POOL_DIRNAME"] = TEST_LOGFILE_DOWNLOAD_POOL_DIRNAME
    environ["BCC_JOB_UPLOAD_POOL_DIRNAME"] = TEST_JOB_UPLOAD_POOL_DIRNAME
    environ["BCC_JOB_PRE_PROC_POOL_DIRNAME"] = TEST_JOB_PRE_PROC_POOL_DIRNAME
    environ["BCC_STORAGE_PREFIX_DIRNAME"] = TEST_STORAGE_PREFIX_DIRNAME
    environ["BCC_JOB_SUPERVISOR_LOG"] = TEST_JOB_SUPERVISOR_LOG
    environ["BCC_EXECUTOR_DATA_DIRNAME"] = TEST_EXECUTOR_DATA_DIRNAME

    environ["BCC_MSS_MACHINE_ROOT_URL"] = TEST_MSS_MACHINE_ROOT_URL
    environ["BCC_MACHINE_ROOT_URL"] = TEST_BCC_MACHINE_ROOT_URL
    environ["BCC_PORT"] = f"{TEST_BCC_PORT}"

    environ["BCC_MSS_APP_TOKEN"] = TEST_MSS_APP_TOKEN
    environ["BCC_QUANTIFY_CONFIG_FILE"] = TEST_QUANTIFY_CONFIG_FILE

    environ["BCC_MAX_TIME_SLOT_LENGTH"] = f"{TEST_MAX_TIME_SLOT_LENGTH}"
    environ["BCC_MIN_TIME_SLOT_LENGTH"] = f"{TEST_MIN_TIME_SLOT_LENGTH}"
    environ["BCC_MAX_SLOTS_PER_DAY"] = f"{TEST_MAX_SLOTS_PER_DAY}"
    environ["BCC_MAX_EXECUTION_TIME"] = f"{TEST_MAX_EXECUTION_TIME}"
    environ["BCC_MAX_POSTPROCESSING_TIME"] = f"{TEST_MAX_POSTPROCESSING_TIME}"
    environ["BCC_MAX_PREPROCESSING_TIME"] = f"{TEST_MAX_PREPROCESSING_TIME}"
    environ["BCC_MAX_GENERAL_QUEUE_TIME"] = f"{TEST_MAX_GENERAL_QUEUE_TIME}"
    environ["BCC_MAX_RECALIBRATION_QUEUE_TIME"] = f"{TEST_MAX_RECALIBRATION_QUEUE_TIME}"
    environ["BCC_MSS_RESPONSE_TIMEOUT"] = f"{TEST_MSS_RESPONSE_TIMEOUT}"
    environ["BCC_MSS_CONNECTION_TIMEOUT"] = f"{TEST_MSS_CONNECTION_TIMEOUT}"
    environ["BCC_MSS_CONNECTION_MAX_ATTEMPTS"] = f"{TEST_MSS_CONNECTION_MAX_ATTEMPTS}"
    environ["BCC_DEFAULT_RECALIBRATION_INTERVAL"] = (
        f"{TEST_DEFAULT_RECALIBRATION_INTERVAL}"
    )
    environ["BCC_MAX_IDLE_TIME"] = f"{TEST_MAX_IDLE_TIME}"
    environ["BCC_IS_ASYNC"] = f"{TEST_IS_ASYNC}"
    environ["BCC_BOOKING_DB_URL"] = f"{TEST_BOOKING_DB_URL}"
    environ["BCC_JWT_SECRET"] = f"{TEST_JWT_SECRET}"
    environ["BCC_JWT_TTL"] = f"{TEST_JWT_TTL}"
    environ["BCC_JOBS_REDIS_URL"] = f"{TEST_JOBS_REDIS_URL}"
    environ["BCC_MSS_PUBLIC_KEY_PATH"] = f"{TEST_MSS_PUBLIC_KEY_PATH}"
    environ["BCC_MSS_NONCE_TTL"] = f"{TEST_MSS_NONCE_TTL}"
    environ["BCC_PRIVATE_KEY_FILE"] = f"{TEST_PRIVATE_KEY_FILE}"
    environ["BCC_CORS_ORIGINS"] = f"{TEST_CORS_ORIGINS}"
    environ["BCC_LOG_LEVEL"] = f"{TEST_LOG_LEVEL}"
    environ["BCC_DEBUG"] = f"{TEST_DEBUG}"

    environ["BCC_REDIS_PORT"] = f"{REDIS_PORT}"
    environ["BCC_REDIS_HOST"] = f"{REDIS_HOST}"
    environ["BCC_REDIS_DB"] = f"{REDIS_DB}"
