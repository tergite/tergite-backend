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
"""Routes for the device's recalibration"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.services import scheduler

from ...services.scheduler import RecalibrationInfo
from ...utils.messages import GeneralMessage
from ..dependencies import (
    DeviceResourcesDep,
    check_device_is_online,
    get_verified_mss_admin_user_id,
)

router = APIRouter()


@router.post(
    "/recalibration/init",
    dependencies=[
        Depends(get_verified_mss_admin_user_id),
        Depends(check_device_is_online),
    ],
)
async def initialize_recalibration(
    device: DeviceResourcesDep,
    start_timestamp: Optional[datetime] = Query(None),
    interval: Optional[float] = Query(None),
) -> GeneralMessage:
    """Initializes recalibration

    Only MSS admin users can initialize recalibration

    Args:
        device: what the device runs on
        start_timestamp: the start time of the recalibration; default = None i.e. straight away
        interval: the recalibration interval; default = None i.e. no repeat

    Returns:
        the status of the recalibration
    """
    return scheduler.init_recalibration(
        device, start_timestamp=start_timestamp, interval=interval
    )


@router.get(
    "/recalibration/info", dependencies=[Depends(get_verified_mss_admin_user_id)]
)
async def get_recalibration_info(device: DeviceResourcesDep) -> RecalibrationInfo:
    """Gets the current information about the recalibration

    Only MSS admin users can get recalibration info
    """
    return scheduler.get_recalibration_info(device)


@router.post(
    "/recalibration/cancel",
    dependencies=[
        Depends(get_verified_mss_admin_user_id),
        Depends(check_device_is_online),
    ],
)
async def cancel_recalibration(device: DeviceResourcesDep) -> GeneralMessage:
    """Cancels recalibration on this device

    Only MSS admin users can initialize recalibration

    Args:
        device: what the device runs on

    Returns:
        general information on the status of the request
    """
    scheduler.stop_recalibration(device)
    return {"status": "success"}
