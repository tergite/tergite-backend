# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020
# (C) Copyright Chalmers Next Labs 2026
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
# - Chalmers Next Labs, 2026: read with pydantic-settings, moved from settings.py
"""The settings of one device, read from its env file and the env variables"""
import importlib.util
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional, Self
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .datetime import get_utc_now

# found, not imported, as importing it takes seconds and hundreds of MB
HAS_QUANTIFY = importlib.util.find_spec("quantify_scheduler") is not None
# the project's root, beside settings.py, where the default files are
_ROOT_PATH = Path(__file__).parents[2]


class DeviceSettings(BaseSettings):
    """The settings of one device, from its env file and the env variables

    Every key is BCC_<NAME>, for the setting <name>. In the environment, BCC_<PREFIX>_<NAME>,
    <PREFIX> being default_prefix in upper case with "_" for any character but letters and
    digits, sets this device's <name>, over BCC_<NAME>, which sets every device's, over the
    env file's BCC_<NAME>. A bare <NAME> is not read, and in the env file is an error.
    """

    model_config = SettingsConfigDict(
        env_prefix="BCC_",
        extra="ignore",
        env_ignore_empty=True,
        validate_default=True,
    )

    default_prefix: str
    app_settings: str = "production"

    storage_root: Path = Path("/tmp")
    storage_prefix_dirname: Optional[str] = None
    logfile_download_pool_dirname: str = "logfile_download_pool"
    job_upload_pool_dirname: str = "job_upload_pool"
    job_pre_proc_pool_dirname: str = "job_preproc_pool"
    job_supervisor_log_name: str = Field(
        "job_supervisor.log", validation_alias="BCC_JOB_SUPERVISOR_LOG"
    )
    executor_data_dirname: str = "executor_data"

    backend_settings: str = str(_ROOT_PATH / "backend_config.toml")
    calibration_seed: str = str(_ROOT_PATH / "calibration.seed.toml")
    calibration_node_config_file: Path = _ROOT_PATH / "node_config.calib.toml"
    calibration_device_config_file: Path = _ROOT_PATH / "device_config.calib.toml"
    calibration_spi_config_file: Path = _ROOT_PATH / "spi_config.calib.toml"

    mss_machine_root_url: str = "http://localhost:8002"
    # the MSS websocket for this device's events; by default MSS's, at /devices/ws/<prefix>
    mss_device_events_endpoint: Optional[str] = None
    mss_connection_timeout: float = 5
    mss_connection_max_attempts: int = 5
    mss_response_timeout: float = 120
    mss_max_reconnection_delay: float = 60
    machine_root_url: str = "http://localhost:8000"
    port: int = 8000

    # MSS public key for encrypting/verifying messages for/from MSS only
    mss_public_key_path: Path = _ROOT_PATH / "public-mss-key.pem"
    # time-to-live for the nonce; defaults to 5 minutes
    mss_nonce_ttl: float = 300
    # how long a job is kept after it last changed; defaults to 2 weeks
    jobs_store_ttl: float = 3600 * 24 * 14
    private_key_file: Path = _ROOT_PATH / "private-bcc-key.pem"
    private_key_password: Optional[bytes] = None

    # what runs the jobs: RQ's queues, or the dispatcher over the SQL database, until one stays
    scheduler: Literal["rq", "dispatcher"] = "rq"
    # which QuantumExecutor runs the jobs; see .env.example
    executor_type: str = "quantify"
    # the cluster or other instrument set-up; see quantify-config.example.yml and
    # quantify-metadata.example.yml
    quantify_config_file: Path = _ROOT_PATH / "quantify-config.json"
    quantify_metadata_file: Path = _ROOT_PATH / "quantify-metadata.yml"
    # whether to write the SPI currents to redis and restore them during circuit execution
    should_restore_currents: bool = False
    is_cluster_reset_allowed: bool = Field(
        False, validation_alias="BCC_ARE_CLUSTERS_RESETTABLE"
    )

    redis_scheme: str = "redis"
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_user: Optional[str] = None
    redis_password: Optional[str] = None
    redis_db: int = 0
    # by default the one of the redis_* settings
    rq_redis_url: Optional[str] = None

    # default: 6 hours
    max_time_slot_length: float = 21600
    # default: 2 minutes
    min_time_slot_length: float = 120
    max_slots_per_day: int = 1
    # default: 15 minutes
    max_idle_time: int = 900
    # default: 10 minutes
    max_execution_time: int = 600
    # default: 5 minutes
    max_preprocessing_time: int = 300
    # default: 3 minutes
    max_postprocessing_time: int = 180
    # default: 3 minutes
    max_general_queue_time: int = 180
    # default: 1.5 hours
    max_recalibration_queue_time: int = 5400
    # default: 1 day
    default_recalibration_interval: float = 86_400
    booking_db_url: str = "sqlite:///booking_db.db"

    jwt_secret: str
    # default: 15 minutes
    jwt_ttl: float = 900
    cors_origins: Annotated[List[str], NoDecode] = []
    log_level: str = "ERROR"
    # a mock current date to make bookings act as if the date now is a given static value;
    # outside production only
    current_date: Optional[datetime] = None

    @field_validator(
        "calibration_node_config_file",
        "calibration_device_config_file",
        "calibration_spi_config_file",
    )
    @classmethod
    def check_calibration_file_exists(cls, path: Path, info: ValidationInfo) -> Path:
        """Refuses a calibration file that is missing where quantify, which reads it, is installed"""
        if HAS_QUANTIFY and not path.exists():
            raise FileNotFoundError(f"BCC_{info.field_name.upper()}: {path} not found.")
        return path

    @field_validator("mss_public_key_path")
    @classmethod
    def check_mss_public_key_exists(cls, path: Path) -> Path:
        path = path.resolve()
        if not path.exists():
            raise ValueError(f"{path} does not exist")
        return path

    @field_validator("private_key_file")
    @classmethod
    def check_private_key_exists(cls, path: Path) -> Path:
        path = path.resolve()
        if not path.exists():
            raise ValueError(f"private key file '{path}' does not exist")
        return path

    @field_validator(
        "max_time_slot_length",
        "min_time_slot_length",
        "max_slots_per_day",
        "max_idle_time",
        "max_execution_time",
        "max_preprocessing_time",
        "max_postprocessing_time",
        "max_general_queue_time",
        "max_recalibration_queue_time",
        "jwt_ttl",
    )
    @classmethod
    def set_negative_limits_to_zero(cls, value: float) -> float:
        """Reads a negative limit as none at all"""
        return max(0, value)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def parse_log_level(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def set_defaults_from_other_settings(self) -> Self:
        """Fills in the settings whose defaults come from others"""
        if self.mss_device_events_endpoint is None:
            self.mss_device_events_endpoint = _get_mss_device_events_endpoint(
                self.mss_machine_root_url, self.default_prefix
            )
        if self.rq_redis_url is None:
            self.rq_redis_url = self.redis_url
        if self.app_settings == "production":
            self.current_date = None
        return self

    def get_utc_now(self) -> datetime:
        """Gets the time now, which BCC_CURRENT_DATE stands in for where set"""
        return self.current_date or get_utc_now()

    @property
    def executor_data_dir(self) -> Path:
        return self.storage_root / self.default_prefix / self.executor_data_dirname

    @property
    def preprocessed_job_pool(self) -> Path:
        return self.storage_root / self.default_prefix / self.job_pre_proc_pool_dirname

    @property
    def job_upload_pool(self) -> Path:
        return self.prefix_root / self.job_upload_pool_dirname

    @property
    def log_file_pool(self) -> Path:
        return self.prefix_root / self.logfile_download_pool_dirname

    @property
    def job_supervisor_log(self) -> Path:
        return self.prefix_root / self.job_supervisor_log_name

    @property
    def prefix_root(self) -> Path:
        """The folder this device shares with those of the same storage prefix"""
        return self.storage_root / (self.storage_prefix_dirname or self.default_prefix)

    @property
    def redis_url(self) -> str:
        """The URL the redis_* settings make up"""
        credentials = ""
        if self.redis_user or self.redis_password:
            credentials = f"{self.redis_user or ''}:{self.redis_password or ''}@"
        return f"{self.redis_scheme}://{credentials}{self.redis_host}:{self.redis_port}/{self.redis_db}"


def read_device_settings(env_file: Path) -> DeviceSettings:
    """Reads a device's settings: its own BCC_<PREFIX>_<NAME> env variables over BCC_<NAME>,
    over the env file's BCC_<NAME>

    Raises:
        ValueError: if the file has a key without the BCC_ prefix, which would be ignored
    """
    file_values = dotenv_values(env_file)
    _check_keys_are_prefixed(env_file, file_values)
    default_prefix = os.environ.get("BCC_DEFAULT_PREFIX") or file_values.get(
        "BCC_DEFAULT_PREFIX"
    )
    # init values, which pydantic-settings reads over the environment and the env file
    device_values = _get_device_env_values(default_prefix)
    return DeviceSettings(_env_file=env_file, **device_values)


def create_device_folders(device_settings: DeviceSettings) -> None:
    """Creates the folders the device keeps its jobs' files in"""
    device_settings.executor_data_dir.mkdir(exist_ok=True, parents=True)
    device_settings.preprocessed_job_pool.mkdir(exist_ok=True, parents=True)
    device_settings.job_upload_pool.mkdir(exist_ok=True, parents=True)
    device_settings.log_file_pool.mkdir(exist_ok=True, parents=True)


def _check_keys_are_prefixed(
    env_file: Path, file_values: dict[str, Optional[str]]
) -> None:
    bare_keys = sorted(key for key in file_values if not key.startswith("BCC_"))
    if bare_keys:
        raise ValueError(
            f"{env_file}: {', '.join(bare_keys)} must be written BCC_<NAME>, "
            f"as only BCC_ prefixed keys are read"
        )


def _get_device_env_values(default_prefix: Optional[str]) -> dict[str, str]:
    """Gets the device's own BCC_<PREFIX>_<NAME> env variables, keyed as DeviceSettings takes them"""
    device_env_prefix = _get_device_env_prefix(default_prefix)
    if device_env_prefix is None:
        return {}
    init_keys = _get_init_keys()
    device_values = {}
    for name, value in os.environ.items():
        env_name = f"BCC_{name.removeprefix(device_env_prefix)}"
        if name.startswith(device_env_prefix) and value != "" and env_name in init_keys:
            device_values[init_keys[env_name]] = value
    return device_values


def _get_init_keys() -> dict[str, str]:
    """Gets the key DeviceSettings takes each setting by, by the setting's env name"""
    init_keys = {}
    for field_name, field in DeviceSettings.model_fields.items():
        if field.validation_alias is None:
            init_keys[f"BCC_{field_name.upper()}"] = field_name
        else:
            init_keys[f"{field.validation_alias}"] = f"{field.validation_alias}"
    return init_keys


def _get_device_env_prefix(default_prefix: Optional[str]) -> Optional[str]:
    """Gets BCC_<PREFIX>_, <PREFIX> being the default prefix in upper case with "_" for any
    character but letters and digits"""
    if not default_prefix:
        return None
    return f"BCC_{re.sub('[^A-Z0-9]', '_', default_prefix.upper())}_"


def _get_mss_device_events_endpoint(
    mss_machine_root_url: str, default_prefix: str
) -> str:
    """Gets MSS's websocket for this device's events, at the host of MSS's URL"""
    mss_url = urlsplit(mss_machine_root_url)
    scheme = "wss" if mss_url.scheme in ("https", "wss") else "ws"
    port = f":{mss_url.port}" if mss_url.port else ""
    return f"{scheme}://{mss_url.hostname}{port}/devices/ws/{default_prefix}"
