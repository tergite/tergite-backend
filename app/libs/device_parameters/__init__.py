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
from typing import TYPE_CHECKING

from redis import Redis

from ...utils.redis_store import Collection
from .dtos import (
    BackendConfig,
    Device,
    DeviceCalibration,
)

if TYPE_CHECKING:
    from ...services.external.mss.service import (
        AsyncMssClientPipe,
    )


def get_backend_config(
    connection: Redis,
    backend_name: str,
) -> BackendConfig:
    """Retrieves the saved backend configuration of the backend of given name

    Args:
        connection: connection to the redis server
        backend_name: name of the backend

    Returns:
        the configuration of the backend
    """
    configs_store = Collection(connection, schema=BackendConfig)
    return configs_store.get_one(backend_name)


def get_device_info(
    connection: Redis,
    backend_name: str,
) -> Device:
    """Retrieves this device's info in Device format

    Args:
        connection: connection to the redis server
        backend_name: the name of the backend

    Returns:
        the device info of the device

    Raises:
        ItemNotFoundError: '{backend_name}' not found
    """
    devices_store = Collection(connection, schema=Device)
    return devices_store.get_one(backend_name)


def get_device_calibration_info(
    connection: Redis,
    backend_name: str,
) -> DeviceCalibration:
    """Retrieves this device's calibration info in DeviceCalibration format

    Args:
        connection: connection to the redis server
        backend_name: the name of the backend

    Returns:
        the DeviceCalibration info of the device

    Raises:
        ItemNotFoundError: '{backend_config.general_config.name}' not found
    """
    calib_store = Collection(connection, schema=DeviceCalibration)
    return calib_store.get_one(backend_name)


async def save_all_device_params(
    redis: Redis, backend_config: BackendConfig, mss_client_pipe: "AsyncMssClientPipe"
):
    """Saves all device parameters in both redis and in MSS

    Args:
        redis: connection to redis
        backend_config: the configuration of the backend
        mss_client_pipe: the pipe to the MSS client

    Raises:
        ValueError: error message from MSS when it attempts to update mss
    """
    device_info = Device.from_config(backend_config)
    calib_info = DeviceCalibration.from_config(backend_config)

    save_device_info(redis, data=device_info)
    save_calibration_info(redis, data=calib_info)
    save_backend_config(redis, data=backend_config)

    # update MSS of this backend's configuration
    await send_device_params_to_mss(
        mss_client_pipe, device_info=device_info, calibration_info=calib_info
    )


def save_device_info(redis: Redis, data: Device):
    """Saves the device information in redis

    Args:
        redis: connection to redis
        data: the static device parameters of the backend
    """
    devices_db = Collection[Device](redis, schema=Device)
    devices_db.insert(data)


def save_calibration_info(redis: Redis, data: DeviceCalibration):
    """Saves the calibration information in redis

    Args:
        redis: connection to redis
        data: the dynamic device parameters of the backend
    """
    calib_db = Collection[DeviceCalibration](redis, schema=DeviceCalibration)
    calib_db.insert(data)


def save_backend_config(connection: Redis, data: BackendConfig):
    """Saves the current system's backend configuration into the database

    Args:
        connection: connection to the redis server
        data: the configuration of the backend
    """
    configs_store = Collection(connection, schema=BackendConfig)
    configs_store.insert(data)


async def send_device_params_to_mss(
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
