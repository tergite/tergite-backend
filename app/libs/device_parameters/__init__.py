# This code is part of Tergite
#
# (C) Copyright Abdullah-Al Amin 2023
# (C) Copyright Martin Ahindura 2024
# (C) Copyright Adilet Tuleouv 2024
# (C) Copyright Chalmers Next Labs AB 2026
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
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from redis import Redis

import settings

from ...utils.redis_store import Collection
from .dtos import (
    BackendConfig,
    Device,
    DeviceCalibration,
)

if TYPE_CHECKING:
    from ...services.external.mss.service import (
        AsyncMssClientPipe,
        BaseMssClientPipe,
        MssClientPipe,
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


async def async_initialize_backend(
    redis: Redis, backend_config: BackendConfig, mss_client_pipe: "AsyncMssClientPipe"
):
    """Runs a number of operations to initialize the backend asynchronously

    Args:
        redis: connection to redis
        backend_config: the configuration of the backend
        mss_client_pipe: the pipe to the MSS client

    Raises:
        ValueError: error message from MSS when it attempts to update mss
        ItemNotFoundError:
    """
    device_info = _save_device_info(redis, backend_config=backend_config)
    calib_info = _save_calibration_info(redis, backend_config=backend_config)

    # update MSS of this backend's configuration
    await async_send_backend_info_to_mss(
        mss_client_pipe,
        device_info=device_info,
        calibration_info=calib_info,
    )


def initialize_backend(
    redis: Redis, backend_config: BackendConfig, mss_client_pipe: "MssClientPipe"
):
    """Runs a number of operations to initialize the backend

    Args:
        redis: connection to redis
        backend_config: the configuration of the backend
        mss_client_pipe: the pipe to the MSS client

    Raises:
        ValueError: error message from MSS when it attempts to update mss
        ItemNotFoundError:
    """
    device_info = _save_device_info(redis, backend_config=backend_config)
    calib_info = _save_calibration_info(redis, backend_config=backend_config)
    # update MSS of this backend's configuration
    send_backend_info_to_mss(
        mss_client_pipe,
        device_info=device_info,
        calibration_info=calib_info,
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


async def async_send_backend_info_to_mss(
    mss_client_pipe: "AsyncMssClientPipe",
    device_info: Device,
    calibration_info: DeviceCalibration,
):
    """
    Sends this backend's information to MSS

    Args:
        mss_client_pipe: the pipe connected to the MSS client
        device_info: the static device info to send to the MSS
        calibration_info: the dynamic device properties to send to MSS

    Raises:
        ValueError: error message from MSS
    """
    from ...services.external.mss.dtos import DeviceEvent, DeviceEventName

    initialization_event = DeviceEvent(
        name=DeviceEventName.INITIALIZED,
        data=device_info,
    )
    recalibration_event = DeviceEvent(
        name=DeviceEventName.RECALIBRATED,
        data=calibration_info,
    )

    await mss_client_pipe.send_event(
        initialization_event, error_prefix="error sending initialization info: "
    )

    await mss_client_pipe.send_event(
        recalibration_event, error_prefix="error sending recalibration info: "
    )


def send_backend_info_to_mss(
    mss_client_pipe: "MssClientPipe",
    device_info: Device,
    calibration_info: DeviceCalibration,
):
    """
    Sends this backend's information to MSS

    Args:
        mss_client_pipe: the pipe connected to the MSS client
        device_info: the static device info to send to the MSS
        calibration_info: the dynamic device properties to send to MSS

    Raises:
        ValueError: error message from MSS
    """
    from ...services.external.mss.dtos import DeviceEvent, DeviceEventName

    initialization_event = DeviceEvent(
        name=DeviceEventName.INITIALIZED,
        data=device_info,
    )
    recalibration_event = DeviceEvent(
        name=DeviceEventName.RECALIBRATED,
        data=calibration_info,
    )

    mss_client_pipe.send_event(
        initialization_event, error_prefix="error sending initialization info: "
    )

    mss_client_pipe.send_event(
        recalibration_event, error_prefix="error sending recalibration info: "
    )


def _save_device_info(redis: Redis, backend_config: BackendConfig) -> Device:
    """Saves the device information in redis given backend config

    Args:
        redis: connection to redis
        backend_config: the configuration of the backend

    Returns:
        the saved device information
    """
    devices_db = Collection[Device](redis, schema=Device)
    device_info = Device.from_config(backend_config)
    devices_db.insert(device_info)
    return device_info


def _save_calibration_info(
    redis: Redis, backend_config: BackendConfig
) -> DeviceCalibration:
    """Saves the calibration information in redis given backend config

    Args:
        redis: connection to redis
        backend_config: the configuration of the backend

    Returns:
        the saved calibration information
    """
    calib_db = Collection[DeviceCalibration](redis, schema=DeviceCalibration)

    try:
        calib_info = DeviceCalibration.from_config(backend_config)
        calib_db.insert(calib_info)
    except ValueError:
        device_info = Device.from_config(backend_config)
        calib_info = calib_db.get_one(device_info.name)
    return calib_info
