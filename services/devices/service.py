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

from sqlalchemy import Engine
from sqlmodel import Session, col, delete

from libs.device_parameters import BackendConfig, Device, DeviceCalibration
from utils.datetime import get_utc_now
from utils.exc import ItemNotFoundError

from ..external.mss import (
    DeviceEvent,
    DeviceEventName,
    DeviceSwitchData,
    MssClient,
    send_mss_event,
)
from .store import DeviceParametersRow, SwitchedOffDeviceRow


def get_device_info(db_engine: Engine, backend_name: str) -> Device:
    """Gets the backend's device info

    Args:
        db_engine: the engine of the device's database
        backend_name: the name of the backend

    Returns:
        the device's static properties

    Raises:
        ItemNotFoundError: '{backend_name}' not found
    """
    row = _get_parameters_row(db_engine, backend_name)
    return Device.model_validate_json(row.device_info)


def get_device_calibration_info(
    db_engine: Engine, backend_name: str
) -> DeviceCalibration:
    """Gets the backend's calibration info

    Args:
        db_engine: the engine of the device's database
        backend_name: the name of the backend

    Returns:
        the device's latest calibration

    Raises:
        ItemNotFoundError: '{backend_name}' not found
    """
    row = _get_parameters_row(db_engine, backend_name)
    return DeviceCalibration.model_validate_json(row.calibration_info)


def save_all_device_params(
    db_engine: Engine,
    backend_config: BackendConfig,
    calibration: DeviceCalibration,
    mss_client: MssClient,
) -> None:
    """Saves the backend's device info and calibration info, and sends them to MSS, or keeps
    them for it while it cannot be reached

    Args:
        db_engine: the engine of the device's database
        backend_config: the backend config the device info is got from
        calibration: the device's calibration, with its frames and defcals where it has
            them
        mss_client: the client to MSS
    """
    device_info = Device.from_config(backend_config)

    with Session(db_engine) as session:
        session.merge(
            DeviceParametersRow(
                backend_name=device_info.name,
                device_info=device_info.model_dump_json(),
                calibration_info=calibration.model_dump_json(),
            )
        )
        session.commit()

    send_mss_event(
        db_engine,
        mss_client,
        DeviceEvent(name=DeviceEventName.INITIALIZED, data=device_info),
    )
    send_mss_event(
        db_engine,
        mss_client,
        DeviceEvent(name=DeviceEventName.RECALIBRATED, data=calibration),
    )


def save_device_calibration(
    db_engine: Engine,
    backend_name: str,
    calibration: DeviceCalibration,
    mss_client: MssClient,
) -> None:
    """Saves the backend's new calibration, which the light workers discriminate with and the
    API serves, and sends it to MSS, or keeps it for it while it cannot be reached

    Args:
        db_engine: the engine of the device's database
        backend_name: the name of the backend
        calibration: the new calibration
        mss_client: the client to MSS

    Raises:
        ItemNotFoundError: '{backend_name}' not found
    """
    with Session(db_engine) as session:
        row = session.get(DeviceParametersRow, backend_name)
        if row is None:
            raise ItemNotFoundError(f"'{backend_name}' not found")
        row.calibration_info = calibration.model_dump_json()
        session.add(row)
        session.commit()
    send_mss_event(
        db_engine,
        mss_client,
        DeviceEvent(name=DeviceEventName.RECALIBRATED, data=calibration),
    )


def switch_off(db_engine: Engine, mss_client: MssClient, device_name: str) -> None:
    """Switches off the device, and tells MSS

    Args:
        db_engine: the engine of the device's database
        mss_client: the client to MSS
        device_name: the name of the device

    Raises:
        MssRejectedError: error sending switch off data: {error message}
        MssTimeoutError: error sending switch off data: response took longer than timeout
    """
    with Session(db_engine) as session:
        session.merge(SwitchedOffDeviceRow(device_name=device_name))
        session.commit()
    mss_client.send_event(
        DeviceEvent(
            name=DeviceEventName.SWITCHED_OFF,
            data=DeviceSwitchData(date=get_utc_now()),
        ),
        error_prefix="error sending switch off data: ",
    )


def switch_on(db_engine: Engine, mss_client: MssClient, device_name: str) -> None:
    """Switches on the device, and tells MSS

    Args:
        db_engine: the engine of the device's database
        mss_client: the client to MSS
        device_name: the name of the device

    Raises:
        MssRejectedError: error sending switch on data: {error message}
        MssTimeoutError: error sending switch on data: response took longer than timeout
    """
    with Session(db_engine) as session:
        session.exec(
            delete(SwitchedOffDeviceRow).where(
                col(SwitchedOffDeviceRow.device_name) == device_name
            )
        )
        session.commit()
    mss_client.send_event(
        DeviceEvent(
            name=DeviceEventName.SWITCHED_ON,
            data=DeviceSwitchData(date=get_utc_now()),
        ),
        error_prefix="error sending switch on data: ",
    )


def is_offline(db_engine: Engine, device_name: str) -> bool:
    """Checks if the named device is switched off

    Args:
        db_engine: the engine of the device's database
        device_name: the name of the device

    Returns:
        True if the device is switched off
    """
    with Session(db_engine) as session:
        return session.get(SwitchedOffDeviceRow, device_name) is not None


def _get_parameters_row(db_engine: Engine, backend_name: str) -> DeviceParametersRow:
    with Session(db_engine) as session:
        row = session.get(DeviceParametersRow, backend_name)
    if row is None:
        raise ItemNotFoundError(f"'{backend_name}' not found")
    return row
