# This code is part of Tergite
#
# (C) Copyright Abdullah-Al Amin 2023
# (C) Copyright Martin Ahindura 2024
# (C) Copyright Adilet Tuleouv 2024
# (C) Nicklas Botö, Fabian Forslund 2022
# (C) Chalmers Next Labs 2025
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
# - Martin Ahindura, 2023, 2025
# - Stefan Hill, 2024
# - Adilet Tuleuov, 2025
# - Chalmers Next Labs, 2026: gathered from app/libs/device_parameters and
#   app/services/scheduler/service.py
"""The device's properties and calibration, stored and published to MSS, and its switch"""
from redis import Redis

from ...libs.device_parameters import BackendConfig, Device, DeviceCalibration
from ...libs.queues.dtos import QueueContext
from ...utils.datetime import get_utc_now
from ...utils.redis import get_redis_connection
from ...utils.redis_store import Collection
from ..external.mss import (
    DeviceEvent,
    DeviceEventName,
    DeviceSwitchData,
    MssClient,
    get_mss_client,
)


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


def save_all_device_params(
    redis: Redis, backend_config: BackendConfig, mss_client: MssClient
) -> None:
    """Saves all device parameters in both redis and in MSS

    Args:
        redis: connection to redis
        backend_config: the configuration of the backend
        mss_client: the MSS client

    Raises:
        ValueError: error message from MSS when it attempts to update mss
    """
    device_info = Device.from_config(backend_config)
    calib_info = DeviceCalibration.from_config(backend_config)

    _save_device_info(redis, data=device_info)
    _save_calibration_info(redis, data=calib_info)

    # update MSS of this backend's configuration
    _send_device_params_to_mss(
        mss_client, device_info=device_info, calibration_info=calib_info
    )


def switch_off(
    context: QueueContext,
) -> None:
    """Switches off the scheduler

    Args:
        context: the context of the queues

    Raises:
        MssRejectedError – error sending switch off data: {error message}
        MssTimeoutError – error sending switch off data: response took longer than timeout
    """
    get_redis_connection().set(_get_offline_flag_key(context), 1)

    # update MSS
    mss_client = get_mss_client()
    mss_client.send_event(
        DeviceEvent(
            name=DeviceEventName.SWITCHED_OFF,
            data=DeviceSwitchData(date=get_utc_now()),
        ),
        error_prefix="error sending switch off data: ",
    )


def switch_on(
    context: QueueContext,
) -> None:
    """Switches on the scheduler

    Args:
        context: the context of the queues

    Raises:
        MssRejectedError – error sending switch on data: {error message}
        MssTimeoutError – error sending switch on data: response took longer than timeout
    """
    get_redis_connection().delete(_get_offline_flag_key(context))

    # update MSS
    mss_client = get_mss_client()
    mss_client.send_event(
        DeviceEvent(
            name=DeviceEventName.SWITCHED_ON,
            data=DeviceSwitchData(date=get_utc_now()),
        ),
        error_prefix="error sending switch on data: ",
    )


def is_offline(context: QueueContext) -> bool:
    """Checks if the device of the given queue context is switched off"""
    return get_redis_connection().exists(_get_offline_flag_key(context)) == 1


def _get_offline_flag_key(context: QueueContext) -> str:
    """Gets the key of the flag set while the device is switched off, one per device"""
    return f"{context['queue_prefix']}:offline"


def _save_device_info(redis: Redis, data: Device) -> None:
    """Saves the device information in redis

    Args:
        redis: connection to redis
        data: the static device parameters of the backend
    """
    devices_db = Collection[Device](redis, schema=Device)
    devices_db.insert(data)


def _save_calibration_info(redis: Redis, data: DeviceCalibration) -> None:
    """Saves the calibration information in redis

    Args:
        redis: connection to redis
        data: the dynamic device parameters of the backend
    """
    calib_db = Collection[DeviceCalibration](redis, schema=DeviceCalibration)
    calib_db.insert(data)


def _send_device_params_to_mss(
    mss_client: MssClient,
    device_info: Device,
    calibration_info: DeviceCalibration,
) -> None:
    """
    Sends this backend's information to MSS

    Args:
        mss_client: the MSS client
        device_info: the static device info to send to the MSS
        calibration_info: the dynamic device properties to send to MSS

    Raises:
        ValueError: error message from MSS
    """
    initialization_event = DeviceEvent(
        name=DeviceEventName.INITIALIZED,
        data=device_info,
    )
    recalibration_event = DeviceEvent(
        name=DeviceEventName.RECALIBRATED,
        data=calibration_info,
    )

    mss_client.send_event(
        initialization_event, error_prefix="error sending initialization info: "
    )

    mss_client.send_event(
        recalibration_event, error_prefix="error sending recalibration info: "
    )
