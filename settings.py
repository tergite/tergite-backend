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
"""The settings of each device this process serves, read from its env file

`settings.X` is X of the current device: the one using_device names for the request or
job at hand, or else the default device, the first loaded. One process serves several
devices when load_devices is given several env files.
"""
import importlib.util
import logging
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Mapping, Optional

from starlette.config import Config
from starlette.datastructures import URL, CommaSeparatedStrings

# found, not imported, as importing them takes seconds and hundreds of MB
HAS_QISKIT_DYNAMICS = importlib.util.find_spec("qiskit_dynamics") is not None
HAS_QUANTIFY = importlib.util.find_spec("quantify_scheduler") is not None

_ROOT_PATH = Path(__file__).parent

# Automatic root directory settings
APP_ROOT_DIR = _ROOT_PATH / "app"
FIXTURES_DIR = _ROOT_PATH / "app" / "tests" / "fixtures"


class DeviceSettings:
    """The settings of one device, from its env file and the env variables

    Every key is BCC_<NAME>, for the setting <NAME>. In the environment, BCC_<PREFIX>_<NAME>,
    <PREFIX> being DEFAULT_PREFIX in upper case with "_" for any character but letters and
    digits, sets this device's <NAME>, over BCC_<NAME>, which sets every device's, over the
    env file's BCC_<NAME>. A bare <NAME> is not read, and in the env file is an error.
    DEFAULT_PREFIX comes from BCC_DEFAULT_PREFIX in the environment or the file.
    """

    def __init__(self, env_file: Path):
        config = _read_config(env_file)

        self.DEFAULT_PREFIX = config("BCC_DEFAULT_PREFIX", cast=str)

        # Misc settings
        self.APP_SETTINGS = config("BCC_APP_SETTINGS", cast=str, default="production")

        # Storage settings
        self.STORAGE_ROOT: Path = config(
            "BCC_STORAGE_ROOT", cast=Path, default=Path("/tmp")
        )
        self.STORAGE_PREFIX_DIRNAME = config(
            "BCC_STORAGE_PREFIX_DIRNAME", cast=str, default=self.DEFAULT_PREFIX
        )

        logfile_download_pool_dirname = config(
            "BCC_LOGFILE_DOWNLOAD_POOL_DIRNAME", cast=str, default="logfile_download_pool"
        )
        job_upload_pool_dirname = config(
            "BCC_JOB_UPLOAD_POOL_DIRNAME", cast=str, default="job_upload_pool"
        )
        job_pre_proc_pool_dirname = config(
            "BCC_JOB_PRE_PROC_POOL_DIRNAME", cast=str, default="job_preproc_pool"
        )
        job_supervisor_log = config(
            "BCC_JOB_SUPERVISOR_LOG", cast=str, default="job_supervisor.log"
        )
        executor_data_dirname = config(
            "BCC_EXECUTOR_DATA_DIRNAME", cast=str, default="executor_data"
        )

        device_root = self.STORAGE_ROOT / self.DEFAULT_PREFIX
        prefix_root = self.STORAGE_ROOT / self.STORAGE_PREFIX_DIRNAME

        self.EXECUTOR_DATA_DIR = device_root / executor_data_dirname
        self.EXECUTOR_DATA_DIR.mkdir(exist_ok=True, parents=True)

        self.PREPROCESSED_JOB_POOL = device_root / job_pre_proc_pool_dirname
        self.PREPROCESSED_JOB_POOL.mkdir(exist_ok=True, parents=True)

        self.JOB_UPLOAD_POOL = prefix_root / job_upload_pool_dirname
        self.JOB_UPLOAD_POOL.mkdir(exist_ok=True, parents=True)

        self.LOG_FILE_POOL = prefix_root / logfile_download_pool_dirname
        self.LOG_FILE_POOL.mkdir(exist_ok=True, parents=True)

        self.JOB_SUPERVISOR_LOG = prefix_root / job_supervisor_log

        # Definition of backend property names
        self.BACKEND_SETTINGS = config(
            "BCC_BACKEND_SETTINGS", cast=str, default=_ROOT_PATH / "backend_config.toml"
        )

        self.CALIBRATION_SEED = config(
            "BCC_CALIBRATION_SEED", cast=str, default=_ROOT_PATH / "calibration.seed.toml"
        )

        self.CALIBRATION_NODE_CONFIG_FILE = _existing_calibration_file(
            config, "BCC_CALIBRATION_NODE_CONFIG_FILE", "node_config.calib.toml"
        )
        self.CALIBRATION_DEVICE_CONFIG_FILE = _existing_calibration_file(
            config, "BCC_CALIBRATION_DEVICE_CONFIG_FILE", "device_config.calib.toml"
        )
        self.CALIBRATION_SPI_CONFIG_FILE = _existing_calibration_file(
            config, "BCC_CALIBRATION_SPI_CONFIG_FILE", "spi_config.calib.toml"
        )

        # Connectivity settings
        self.MSS_MACHINE_ROOT_URL: URL = config(
            "BCC_MSS_MACHINE_ROOT_URL", cast=URL, default="http://localhost:8002"
        )
        # The MSS domain, stripping the ':None' if no port is passed
        mss_url = self.MSS_MACHINE_ROOT_URL
        mss_port_ext = f":{mss_url.port}" if mss_url.port else ""
        mss_ws_scheme = "wss" if mss_url.is_secure else "ws"
        default_mss_ws_url = f"{mss_ws_scheme}://{mss_url.hostname}{mss_port_ext}/devices/ws/{self.DEFAULT_PREFIX}"

        # The endpoint for sending device events
        self.MSS_DEVICE_EVENTS_ENDPOINT: URL = config(
            "BCC_MSS_DEVICE_EVENTS_ENDPOINT", cast=URL, default=default_mss_ws_url
        )
        self.MSS_CONNECTION_TIMEOUT = config(
            "BCC_MSS_CONNECTION_TIMEOUT", cast=float, default=5
        )
        self.MSS_CONNECTION_MAX_ATTEMPTS = config(
            "BCC_MSS_CONNECTION_MAX_ATTEMPTS", cast=int, default=5
        )
        self.MSS_RESPONSE_TIMEOUT = config(
            "BCC_MSS_RESPONSE_TIMEOUT", cast=float, default=120
        )
        self.MSS_MAX_RECONNECTION_DELAY = config(
            "BCC_MSS_MAX_RECONNECTION_DELAY", cast=float, default=60
        )

        self.MACHINE_ROOT_URL = config(
            "BCC_MACHINE_ROOT_URL", cast=URL, default="http://localhost:8000"
        )
        self.PORT = config("BCC_PORT", cast=int, default=8000)

        # Authentication

        # MSS public key for encrypting/verifying messages for/from MSS only
        self.MSS_PUBLIC_KEY_PATH = config(
            "BCC_MSS_PUBLIC_KEY_PATH", cast=Path, default=_ROOT_PATH / "public-mss-key.pem"
        ).resolve()
        if not self.MSS_PUBLIC_KEY_PATH.exists():
            raise ValueError(f"{self.MSS_PUBLIC_KEY_PATH} does not exist")

        # time-to-live for the nonce; defaults to 5 minutes
        self.MSS_NONCE_TTL = config("BCC_MSS_NONCE_TTL", cast=float, default=300)

        # time to live for the request logs; defaults to 2 weeks
        self.REQUEST_LOG_TTL = config(
            "BCC_REQUEST_LOG_TTL", cast=float, default=3600 * 24 * 14
        )

        # time interval between consecutive cleanups of expired indexes; defaults to 5 hours
        self.REQUEST_LOG_CLEAN_INTERVAL = config(
            "BCC_REQUEST_LOG_CLEAN_INTERVAL", cast=float, default=3600 * 5
        )

        # time to live for the jobs store; defaults to 2 weeks
        self.JOBS_STORE_TTL = config(
            "BCC_JOBS_STORE_TTL", cast=float, default=3600 * 24 * 14
        )

        # time interval between consecutive cleanups of expired indexes; defaults to 5 hours
        self.JOBS_STORE_CLEAN_INTERVAL = config(
            "BCC_JOBS_STORE_CLEAN_INTERVAL", cast=float, default=3600 * 5
        )

        self.PRIVATE_KEY_FILE = config(
            "BCC_PRIVATE_KEY_FILE", cast=Path, default=_ROOT_PATH / "private-bcc-key.pem"
        ).resolve()
        if not self.PRIVATE_KEY_FILE.exists():
            raise ValueError(
                f"private key file '{self.PRIVATE_KEY_FILE}' does not exist"
            )

        self.PRIVATE_KEY_PASSWORD: Optional[bytes] = None
        private_key_password: Optional[str] = config(
            "BCC_PRIVATE_KEY_PASSWORD", default=None, cast=str
        )
        if private_key_password is not None:
            self.PRIVATE_KEY_PASSWORD = private_key_password.encode("utf-8")

        # -----------------------
        # Hardware configurations
        # -----------------------
        # The executor type specifies which implementations of the QuantumExecutor to use.
        # For more information on the values check:
        # - .env.example
        self.EXECUTOR_TYPE = config("BCC_EXECUTOR_TYPE", default="quantify")

        # This will load the hardware configuration from a yaml file, which contains the properties for the
        # cluster or other instrument setup. For more information check:
        # - quantify-config.example.yml
        # - quantify-metadata.example.yml
        self.QUANTIFY_CONFIG_FILE = config(
            "BCC_QUANTIFY_CONFIG_FILE",
            cast=Path,
            default=_ROOT_PATH / "quantify-config.json",
        )
        self.QUANTIFY_METADATA_FILE = config(
            "BCC_QUANTIFY_METADATA_FILE",
            cast=Path,
            default=_ROOT_PATH / "quantify-metadata.yml",
        )

        # If set to true, it should write currents to redis and return them to previous values during circuit execution
        self.SHOULD_RESTORE_CURRENTS = config(
            "BCC_SHOULD_RESTORE_CURRENTS", cast=bool, default=False
        )
        self.ARE_CLUSTERS_RESETTABLE = config(
            "BCC_ARE_CLUSTERS_RESETTABLE", cast=bool, default=False
        )

        # -------------
        # Redis config
        # -------------
        self.REDIS_SCHEME = config("BCC_REDIS_SCHEME", default="redis")
        self.REDIS_HOST = config("BCC_REDIS_HOST", default="localhost")
        self.REDIS_PORT = config("BCC_REDIS_PORT", default=6379)
        self.REDIS_USER = config("BCC_REDIS_USER", default=None)
        self.REDIS_PASSWORD = config("BCC_REDIS_PASSWORD", default=None)
        self.REDIS_DB = config("BCC_REDIS_DB", cast=int, default=0)

        # Queue config

        # default: 6 hours
        self.MAX_TIME_SLOT_LENGTH = max(
            0.0, config("BCC_MAX_TIME_SLOT_LENGTH", cast=float, default=21600)
        )

        # default: 2 minutes
        self.MIN_TIME_SLOT_LENGTH = max(
            0.0, config("BCC_MIN_TIME_SLOT_LENGTH", cast=float, default=120)
        )

        # default: 1
        self.MAX_SLOTS_PER_DAY = max(
            0, config("BCC_MAX_SLOTS_PER_DAY", cast=int, default=1)
        )

        # default: 15 minutes
        self.MAX_IDLE_TIME = max(0, config("BCC_MAX_IDLE_TIME", cast=int, default=900))

        # default: 10 minutes
        self.MAX_EXECUTION_TIME = max(
            0, config("BCC_MAX_EXECUTION_TIME", cast=int, default=600)
        )

        # default: 5 minutes
        self.MAX_PREPROCESSING_TIME = max(
            0, config("BCC_MAX_PREPROCESSING_TIME", cast=int, default=300)
        )

        # default: 3 minutes
        self.MAX_POSTPROCESSING_TIME = max(
            0, config("BCC_MAX_POSTPROCESSING_TIME", cast=int, default=180)
        )

        # default: 3 minutes
        self.MAX_GENERAL_QUEUE_TIME = max(
            0, config("BCC_MAX_GENERAL_QUEUE_TIME", cast=int, default=180)
        )

        # default: 1.5 hours
        self.MAX_RECALIBRATION_QUEUE_TIME = max(
            0, config("BCC_MAX_RECALIBRATION_QUEUE_TIME", cast=int, default=5400)
        )

        # default: 1 day
        self.DEFAULT_RECALIBRATION_INTERVAL = config(
            "BCC_DEFAULT_RECALIBRATION_INTERVAL", cast=float, default=86_400
        )

        # default: True
        self.IS_ASYNC = config("BCC_IS_ASYNC", cast=bool, default="True")

        # default: "sqlite:///booking_db.db"
        self.BOOKING_DB_URL = config(
            "BCC_BOOKING_DB_URL", default="sqlite:///booking_db.db"
        )

        # default: "redis://localhost:6379/0"
        redis_credentials = ""
        if self.REDIS_USER or self.REDIS_PASSWORD:
            redis_credentials = f"{self.REDIS_USER or ''}:{self.REDIS_PASSWORD or ''}@"
        default_redis_url = f"{self.REDIS_SCHEME}://{redis_credentials}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        self.RQ_REDIS_URL = config("BCC_RQ_REDIS_URL", default=default_redis_url)

        self.JWT_SECRET = config("BCC_JWT_SECRET")

        # default: 900
        self.JWT_TTL = max(0.0, config("BCC_JWT_TTL", cast=float, default=900))

        self.CORS_ORIGINS: List[str] = config(
            "BCC_CORS_ORIGINS", cast=CommaSeparatedStrings, default=[]
        )

        self.LOG_LEVEL = config("BCC_LOG_LEVEL", default="ERROR").strip().upper()

        # a mock current date to make bookings act as if the date now is a given static value
        self.CURRENT_DATE: Optional[datetime] = None
        current_date = config("BCC_CURRENT_DATE", default="")
        if current_date and self.APP_SETTINGS != "production":
            self.CURRENT_DATE = datetime.fromisoformat(current_date)


# FIXME: This __getattr__ indirection will confuse readers of the code base. Better if explicitly got.
def __getattr__(name: str):
    return getattr(get_device_settings(), name)


def load_devices(env_files: List[Path]) -> List[str]:
    """Serves the devices of the given env files, the first being the default one

    Returns:
        the names of the devices, i.e. their DEFAULT_PREFIX
    """
    global _DEFAULT_DEVICE, _DEVICES
    devices = [DeviceSettings(Path(env_file).resolve()) for env_file in env_files]
    _DEFAULT_DEVICE = devices[0]
    _DEVICES = {device.DEFAULT_PREFIX: device for device in devices}
    return list(_DEVICES)


def get_device_names() -> List[str]:
    """Gets the names of the devices this process serves, the default one first"""
    return list(_DEVICES)


def get_device_settings(name: Optional[str] = None) -> DeviceSettings:
    """Gets the settings of the named device, or else of the current one"""
    name = name or _CURRENT_DEVICE.get()
    return _DEVICES[name] if name else _DEFAULT_DEVICE


@contextmanager
def using_device(name: str) -> Iterator[DeviceSettings]:
    """Makes the named device the current one within the block"""
    token = _CURRENT_DEVICE.set(name)
    try:
        yield _DEVICES[name]
    finally:
        _CURRENT_DEVICE.reset(token)


def _read_config(env_file: Path) -> Config:
    """Reads the env file, whose keys are BCC_<NAME>, under the variables

    Raises:
        ValueError: if the file has a key without the BCC_ prefix, which would be ignored
    """
    config = Config(env_file, environ=os.environ)
    default_prefix = config("BCC_DEFAULT_PREFIX", cast=str)
    device_specific_env = _get_device_specific_environ(default_prefix)
    config.file_values.update(device_specific_env)

    bare_keys = sorted(key for key in config.file_values if not key.startswith("BCC_"))
    if bare_keys:
        raise ValueError(
            f"{env_file}: {', '.join(bare_keys)} must be written BCC_<NAME>, "
            f"as only BCC_ prefixed keys are read"
        )
    return config

def _get_device_specific_environ(default_prefix: str) -> dict[str, str]:
    """Gets the environment settings specific to a given device.

    Args:
        default_prefix: The default prefix of the device

    Returns:
        the dict of environment settings with the default prefix removed, but still starting with BCC_
    """
    # replace all non alphanumeric characters in the prefix with _
    prefix = f"BCC_{re.sub('[^A-Z0-9]', '_', default_prefix.upper())}_"
    return {f"BCC_{k.removeprefix(prefix)}": v for k, v in os.environ.items() if k.startswith(prefix)}


def _existing_calibration_file(config: Config, key: str, default_name: str) -> Path:
    path = config(key, cast=Path, default=_ROOT_PATH / default_name)
    if HAS_QUANTIFY and not path.exists():
        raise FileNotFoundError(f"{key}: {path} not found.")
    return path


_DEFAULT_DEVICE = DeviceSettings(_ROOT_PATH / os.environ.get("BCC_ENV_FILE", ".env"))
_DEVICES: dict[str, DeviceSettings] = {_DEFAULT_DEVICE.DEFAULT_PREFIX: _DEFAULT_DEVICE}
_CURRENT_DEVICE: ContextVar[Optional[str]] = ContextVar("current_device", default=None)

# setup logger for the app
logging.basicConfig(
    level=getattr(logging, _DEFAULT_DEVICE.LOG_LEVEL, logging.ERROR),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    stream=sys.stdout,
)
