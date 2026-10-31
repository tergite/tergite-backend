# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Routes that switch the device on and off"""
from fastapi import APIRouter, Depends

from services import devices, scheduler
from utils.messages import GeneralMessage

from ..dependencies import (
    DeviceResourcesDep,
    check_device_is_online,
    get_verified_mss_admin_user_id,
    get_verified_mss_user_id,
)

router = APIRouter()


# plain def, so FastAPI runs them in a thread: telling MSS blocks, for up to minutes
@router.post(
    "/switch/off",
    dependencies=[
        Depends(get_verified_mss_admin_user_id),
        Depends(check_device_is_online),
    ],
)
def switch_off(device: DeviceResourcesDep) -> GeneralMessage:
    """Switches off this device

    Only MSS admin users can switch on and off this device

    Args:
        device: what the device runs on

    Returns:
        the status of the request
    """
    devices.switch_off(
        device.bookings_db, device.mss_client, device.settings.default_prefix
    )
    return {"status": "success"}


@router.post("/switch/on", dependencies=[Depends(get_verified_mss_admin_user_id)])
def switch_on(device: DeviceResourcesDep) -> GeneralMessage:
    """Switches on this device

    Only MSS admin users can switch on and off this device

    Args:
        device: what the device runs on

    Returns:
        the status of the request
    """
    devices.switch_on(
        device.bookings_db, device.mss_client, device.settings.default_prefix
    )
    scheduler.wake_workers(device, "heavy")
    return {"status": "success"}


@router.get("/switch/status", dependencies=[Depends(get_verified_mss_user_id)])
async def get_switch_status(device: DeviceResourcesDep):
    """Gets the status of this device

    Args:
        device: what the device runs on

    Returns:
        the status, "ON" or "OFF"
    """
    is_offline = devices.is_offline(device.bookings_db, device.settings.default_prefix)
    status = "OFF" if is_offline else "ON"
    return {"status": status}
