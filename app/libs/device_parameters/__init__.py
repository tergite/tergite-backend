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
# CAUTION: This updater is currently also used in the tergite-autocalibration-lite repository!
# Any change on this file should be done in both repositories until they are eventually merged!
from typing import Optional

from redis import Redis
from requests import Session

import settings

from ...utils.redis_store import Collection
from .dtos import (
    BackendConfig,
    Device,
    DeviceCalibration,
)

_BACKEND_CONFIG: Optional[BackendConfig] = None


def get_backend_config() -> BackendConfig:
    """Returns the current system's backend configuration.

    Loads the static configuration from the backend_config.toml file and
    merges it with seed data (from either qiskit_pulse or quantify seed file)
    validated via Pydantic.
    """
    global _BACKEND_CONFIG
    if _BACKEND_CONFIG is None:
        # Load static configuration from the main backend_config.toml
        _BACKEND_CONFIG = BackendConfig.from_toml(
            settings.BACKEND_SETTINGS,
            seed_file=settings.CALIBRATION_SEED,
        )
    return _BACKEND_CONFIG


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
    redis: Redis,
    backend_config: Optional[BackendConfig] = None,
) -> Device:
    """Retrieves this device's info in Device format

    Args:
        redis: the connection to redis database
        backend_config: the BackendConfig instance for this device

    Returns:
        the device info of the device

    Raises:
        ItemNotFoundError: '{backend_config.general_config.name}' not found
    """
    if backend_config is None:
        backend_config = get_backend_config()

    device_name = backend_config.general_config.name
    devices_db = Collection[Device](redis, schema=Device)
    return devices_db.get_one(device_name)


def get_device_calibration_info(
    redis: Redis,
    backend_config: Optional[BackendConfig] = None,
) -> DeviceCalibration:
    """Retrieves this device's calibration info in DeviceCalibration format

    Args:
        redis: the connection to redis
        backend_config: the BackendConfig instance for this device

    Returns:
        the DeviceCalibration info of the device

    Raises:
        ItemNotFoundError: '{backend_config.general_config.name}' not found
    """
    if backend_config is None:
        backend_config = get_backend_config()

    device_name = backend_config.general_config.name
    calib_db = Collection[DeviceCalibration](redis, schema=DeviceCalibration)
    return calib_db.get_one(device_name)


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
