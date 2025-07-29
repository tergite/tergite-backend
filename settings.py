# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Modified:
# - Stefan Hill, 2024
#

import os
from pathlib import Path
from typing import List

import redis
from starlette.config import Config
from starlette.datastructures import URL, CommaSeparatedStrings

_ROOT_PATH = Path(__file__).parent
# NOTE: shell env variables take precedence over the configuration file
env_file = os.environ.get("ENV_FILE", default=".env")
config = Config(_ROOT_PATH / env_file, environ=os.environ)

# Automatic root directory settings
APP_ROOT_DIR = _ROOT_PATH / "app"
FIXTURES_DIR = _ROOT_PATH / "app" / "tests" / "fixtures"

# Misc settings
APP_SETTINGS = config("APP_SETTINGS", cast=str, default="production")
IS_AUTH_ENABLED = config("IS_AUTH_ENABLED", cast=bool, default=True)
IS_STANDALONE = config("IS_STANDALONE", cast=bool, default=False)
_is_production = APP_SETTINGS == "production"

if not IS_AUTH_ENABLED and _is_production:
    raise ValueError(
        "'IS_AUTH_ENABLED' environment variable has been set to false in production."
    )

# Storage settings
DEFAULT_PREFIX = config("DEFAULT_PREFIX", cast=str)
STORAGE_ROOT: Path = config("STORAGE_ROOT", cast=Path, default=Path("/tmp"))
STORAGE_PREFIX_DIRNAME = config(
    "STORAGE_PREFIX_DIRNAME", cast=str, default=DEFAULT_PREFIX
)

_LOGFILE_DOWNLOAD_POOL_DIRNAME = config(
    "LOGFILE_DOWNLOAD_POOL_DIRNAME", cast=str, default="logfile_download_pool"
)
_JOB_UPLOAD_POOL_DIRNAME = config(
    "JOB_UPLOAD_POOL_DIRNAME", cast=str, default="job_upload_pool"
)
_JOB_PRE_PROC_POOL_DIRNAME = config(
    "JOB_PRE_PROC_POOL_DIRNAME", cast=str, default="job_preproc_pool"
)
_JOB_EXECUTION_POOL_DIRNAME = config(
    "JOB_EXECUTION_POOL_DIRNAME", cast=str, default="job_execution_pool"
)
_JOB_SUPERVISOR_LOG = config(
    "JOB_SUPERVISOR_LOG", cast=str, default="job_supervisor.log"
)
_EXECUTOR_DATA_DIRNAME = config(
    "EXECUTOR_DATA_DIRNAME", cast=str, default="executor_data"
)

EXECUTOR_DATA_DIR = STORAGE_ROOT / DEFAULT_PREFIX / _EXECUTOR_DATA_DIRNAME
EXECUTOR_DATA_DIR.mkdir(exist_ok=True, parents=True)

PREPROCESSED_JOB_POOL = STORAGE_ROOT / DEFAULT_PREFIX / _JOB_PRE_PROC_POOL_DIRNAME
PREPROCESSED_JOB_POOL.mkdir(exist_ok=True, parents=True)

JOB_UPLOAD_POOL = STORAGE_ROOT / STORAGE_PREFIX_DIRNAME / _JOB_UPLOAD_POOL_DIRNAME
JOB_UPLOAD_POOL.mkdir(exist_ok=True, parents=True)

LOG_FILE_POOL = STORAGE_ROOT / STORAGE_PREFIX_DIRNAME / _LOGFILE_DOWNLOAD_POOL_DIRNAME
LOG_FILE_POOL.mkdir(exist_ok=True, parents=True)

JOB_SUPERVISOR_LOG = STORAGE_ROOT / STORAGE_PREFIX_DIRNAME / _JOB_SUPERVISOR_LOG

# Definition of backend property names
BACKEND_SETTINGS = config(
    "BACKEND_SETTINGS",
    cast=str,
    default=_ROOT_PATH / "backend_config.toml",
)

CALIBRATION_SEED = config(
    "CALIBRATION_SEED",
    cast=str,
    default=_ROOT_PATH / "calibration.seed.toml",
)

# Connectivity settings
MSS_MACHINE_ROOT_URL = config(
    "MSS_MACHINE_ROOT_URL", cast=URL, default="http://localhost:8002"
)
BCC_MACHINE_ROOT_URL = config(
    "BCC_MACHINE_ROOT_URL", cast=URL, default="http://localhost:8000"
)
BCC_PORT = config("BCC_PORT", cast=int, default=8000)

# Authentication

MSS_APP_TOKEN = config("MSS_APP_TOKEN", cast=str, default="")
# MSS public key for encrypting/verifying messages for/from MSS only
MSS_PUBLIC_KEY_PATH = config("MSS_PUBLIC_KEY_PATH", cast=Path)
# time-to-live for the nonce; defaults to 5 minutes
MSS_NONCE_TTL = config("MSS_NONCE_TTL", cast=float, default=300)


# -----------------------
# Hardware configurations
# -----------------------
# The executor type specifies which implementations of the QuantumExecutor to use.
# For more information on the values check:
# - dot-env-template.txt
EXECUTOR_TYPE = config("EXECUTOR_TYPE", default="quantify")

# This will load the hardware configuration from a yaml file, which contains the properties for the
# cluster or other instrument setup. For more information check:
# - quantify-config.example.yml
# - quantify-metadata.example.yml
QUANTIFY_CONFIG_FILE = config(
    "QUANTIFY_CONFIG_FILE", cast=Path, default=_ROOT_PATH / "quantify-config.json"
)
QUANTIFY_METADATA_FILE = config(
    "QUANTIFY_METADATA_FILE", cast=Path, default=_ROOT_PATH / "quantify-metadata.yml"
)

# -------------
# Redis config
# -------------
REDIS_HOST = config("REDIS_HOST", default="localhost")
REDIS_PORT = config("REDIS_PORT", default=6379)
REDIS_USER = config("REDIS_USER", default=None)
REDIS_PASSWORD = config("REDIS_PASSWORD", default=None)
REDIS_DB = config("REDIS_DB", cast=int, default=0)


# Queue config

# default: 6 hours
MAX_TIME_SLOT_LENGTH = max(
    0.0, config("MAX_TIME_SLOT_LENGTH", cast=float, default=21600)
)

# default: 2 minutes
MIN_TIME_SLOT_LENGTH = max(0.0, config("MIN_TIME_SLOT_LENGTH", cast=float, default=120))

# default: 1
MAX_SLOTS_PER_DAY = max(0, config("MAX_SLOTS_PER_DAY", cast=int, default=1))

# default: 15 minutes
MAX_IDLE_TIME = max(0, config("MAX_IDLE_TIME", cast=int, default=900))

# default: True
IS_ASYNC = config("IS_ASYNC", cast=bool, default="True")

# default: "sqlite:///booking_db.db"
BOOKING_DB_URL = config("BOOKING_DB_URL", default="sqlite:///booking_db.db")

# default: "redis://localhost:6379"
RQ_REDIS_URL = config("RQ_REDIS_URL", default="redis://localhost:6379/0")
# FIXME: Get rid of this when all queues are shifted over to scheduler
REDIS_CONNECTION = redis.Redis.from_url(RQ_REDIS_URL)

JWT_SECRET = config("JWT_SECRET")

# default: 900
JWT_TTL = max(0.0, config("JWT_TTL", cast=float, default=900))

# default: 5000
REST_API_PORT = max(0, config("REST_API_PORT", cast=int, default=5000))

CORS_ORIGINS: List[str] = config("CORS_ORIGINS", cast=CommaSeparatedStrings, default=[])
