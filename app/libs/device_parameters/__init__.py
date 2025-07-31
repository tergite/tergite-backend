# This code is part of Tergite
#
# (C) Copyright Abdullah-Al Amin 2023
# (C) Copyright Martin Ahindura 2024
# (C) Copyright Adilet Tuleouv 2024
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
#
# - Martin Ahindura, 2023
# - Stefan Hill, 2024
# - Adilet Tuleuov, 2025
#
from typing import Dict, Optional, Tuple

from redis import Redis
from requests import Session

import settings

from ...utils.redis_store import Collection
from .dtos import (
    BackendConfig,
    Device,
    DeviceCalibration,
)

_BACKEND_CONFIGS_CACHE: Dict[Tuple[str, str], BackendConfig] = {}
_DEVICES_STORE_CACHE: Dict[str, Collection[Device]] = {}
_CALIB_STORE_CACHE: Dict[str, Collection[DeviceCalibration]] = {}


def get_backend_config() -> BackendConfig:
    """Returns the current system's backend configuration.

    Loads the static configuration from the backend_config.toml file and
    merges it with seed data (from either qiskit_pulse or quantify seed file)
    validated via Pydantic.
    """
    global _BACKEND_CONFIGS_CACHE
    backend_config_file = settings.BACKEND_SETTINGS
    calib_seed_file = settings.CALIBRATION_SEED

    try:
        return _BACKEND_CONFIGS_CACHE[(backend_config_file, calib_seed_file)]
    except KeyError:
        backend_config = BackendConfig.from_toml(
            backend_config_file,
            seed_file=calib_seed_file,
        )
        _BACKEND_CONFIGS_CACHE[(backend_config_file, calib_seed_file)] = backend_config
        return backend_config


def clear_config_caches():
    """Clears the caches for configurations"""
    global _BACKEND_CONFIGS_CACHE, _DEVICES_STORE_CACHE, _CALIB_STORE_CACHE
    _BACKEND_CONFIGS_CACHE.clear()
    _DEVICES_STORE_CACHE.clear()
    _CALIB_STORE_CACHE.clear()


def initialize_backend(
    redis: Redis,
    backend_config: BackendConfig,
    mss_client: Session,
    mss_url: str,
    is_standalone: bool = settings.IS_STANDALONE,
):
    """Runs a number of operations to initialize the backend

    Args:
        redis: connection to redis
        backend_config: the configuration of the backend
        mss_client: the requests Session to make requests to MSS with
        mss_url: the URL to MSS
        is_standalone: whether this backend is standalone or is connected to an MSS

    Raises:
        ValueError: error message from MSS when it attempts to update mss
        ItemNotFoundError:
    """
    devices_db = Collection[Device](redis, schema=Device)
    calib_db = Collection[DeviceCalibration](redis, schema=DeviceCalibration)

    device_info = Device.from_config(backend_config)
    devices_db.insert(device_info)

    try:
        calib_info = DeviceCalibration.from_config(backend_config)
        calib_db.insert(calib_info)
    except ValueError:
        calib_info = calib_db.get_one(device_info.name)

    if not is_standalone:
        # update MSS of this backend's configuration
        send_backend_info_to_mss(
            mss_client,
            device_info=device_info,
            calibration_info=calib_info,
            mss_url=mss_url,
        )


def get_device_info(
    backend_config: Optional[BackendConfig] = None,
) -> Device:
    """Retrieves this device's info in Device format

    Args:
        backend_config: the BackendConfig instance for this device

    Returns:
        the device info of the device

    Raises:
        ItemNotFoundError: '{backend_config.general_config.name}' not found
    """
    global _DEVICES_STORE_CACHE
    if backend_config is None:
        backend_config = get_backend_config()

    redis_url = settings.RQ_REDIS_URL
    try:
        devices_store = _DEVICES_STORE_CACHE[redis_url]
    except KeyError:
        connection = Redis.from_url(redis_url)
        devices_store = Collection(connection, schema=Device)
        _DEVICES_STORE_CACHE[redis_url] = devices_store

    device_name = backend_config.general_config.name
    return devices_store.get_one(device_name)


def get_device_calibration_info(
    backend_config: Optional[BackendConfig] = None,
) -> DeviceCalibration:
    """Retrieves this device's calibration info in DeviceCalibration format

    Args:
        backend_config: the BackendConfig instance for this device

    Returns:
        the DeviceCalibration info of the device

    Raises:
        ItemNotFoundError: '{backend_config.general_config.name}' not found
    """
    global _CALIB_STORE_CACHE
    if backend_config is None:
        backend_config = get_backend_config()

    redis_url = settings.RQ_REDIS_URL
    try:
        calib_store = _CALIB_STORE_CACHE[redis_url]
    except KeyError:
        connection = Redis.from_url(redis_url)
        calib_store = Collection(connection, schema=DeviceCalibration)
        _CALIB_STORE_CACHE[redis_url] = calib_store

    device_name = backend_config.general_config.name
    return calib_store.get_one(device_name)


def send_backend_info_to_mss(
    mss_client: Session,
    device_info: Device,
    calibration_info: DeviceCalibration,
    mss_url: str = settings.MSS_MACHINE_ROOT_URL,
):
    """
    Sends this backend's information to MSS

    Args:
        mss_client: the requests Session to run the queries
        device_info: the static device info to send to the MSS
        calibration_info: the dynamic device properties to send to MSS
        mss_url: the URL to MSS

    Raises:
        ValueError: error message from MSS
    """
    device_info = device_info.model_dump()
    calibration_info = calibration_info.model_dump()

    responses = [
        mss_client.put(f"{mss_url}/devices/", json=device_info),
        mss_client.post(f"{mss_url}/calibrations/", json=calibration_info),
    ]

    error_message = ",".join([v.text for v in responses if not v.ok])
    if error_message != "":
        raise ValueError(error_message)
