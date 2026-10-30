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
from ...utils.datetime import get_utc_now
from ...utils.exc import ItemNotFoundError
from ..external.mss import (
    DeviceEvent,
    DeviceEventName,
    DeviceSwitchData,
    MssClient,
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
    data = connection.get(_get_device_info_key(backend_name))
    if data is None:
        raise ItemNotFoundError(f"'{backend_name}' not found")
    return Device.model_validate_json(data)


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
        ItemNotFoundError: '{backend_name}' not found
    """
    data = connection.get(_get_calibration_info_key(backend_name))
    if data is None:
        raise ItemNotFoundError(f"'{backend_name}' not found")
    return DeviceCalibration.model_validate_json(data)


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

    redis.set(_get_device_info_key(device_info.name), device_info.model_dump_json())
    redis.set(_get_calibration_info_key(calib_info.name), calib_info.model_dump_json())

    # update MSS of this backend's configuration
    _send_device_params_to_mss(
        mss_client, device_info=device_info, calibration_info=calib_info
    )


def switch_off(redis: Redis, mss_client: MssClient, device_name: str) -> None:
    """Switches off the device, and tells MSS

    Raises:
        MssRejectedError: error sending switch off data: {error message}
        MssTimeoutError: error sending switch off data: response took longer than timeout
    """
    redis.set(_get_offline_flag_key(device_name), 1)
    mss_client.send_event(
        DeviceEvent(
            name=DeviceEventName.SWITCHED_OFF,
            data=DeviceSwitchData(date=get_utc_now()),
        ),
        error_prefix="error sending switch off data: ",
    )


def switch_on(redis: Redis, mss_client: MssClient, device_name: str) -> None:
    """Switches on the device, and tells MSS

    Raises:
        MssRejectedError: error sending switch on data: {error message}
        MssTimeoutError: error sending switch on data: response took longer than timeout
    """
    redis.delete(_get_offline_flag_key(device_name))
    mss_client.send_event(
        DeviceEvent(
            name=DeviceEventName.SWITCHED_ON,
            data=DeviceSwitchData(date=get_utc_now()),
        ),
        error_prefix="error sending switch on data: ",
    )


def is_offline(redis: Redis, device_name: str) -> bool:
    """Checks if the named device is switched off"""
    return redis.exists(_get_offline_flag_key(device_name)) == 1


def _get_offline_flag_key(device_name: str) -> str:
    """Gets the key of the flag set while the device is switched off, one per device"""
    return f"{device_name}:offline"


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


def _get_device_info_key(backend_name: str) -> str:
    """Gets the key of the backend's device info, as JSON"""
    return f"{backend_name}:device-info"


def _get_calibration_info_key(backend_name: str) -> str:
    """Gets the key of the backend's calibration info, as JSON"""
    return f"{backend_name}:calibration-info"
