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
"""Routes for the device's static and dynamic properties"""
from fastapi import APIRouter, Depends

from ...services import devices
from ..dependencies import DeviceResourcesDep, get_verified_mss_user_id

router = APIRouter()


@router.get("/static-properties", dependencies=[Depends(get_verified_mss_user_id)])
async def get_static_properties(device: DeviceResourcesDep):
    """Retrieves the device properties that are not changing"""
    return devices.get_device_info(
        device.bookings_db, device.executor_options.backend_name
    )


@router.get("/dynamic-properties", dependencies=[Depends(get_verified_mss_user_id)])
async def get_dynamic_properties(device: DeviceResourcesDep):
    """Retrieves the device properties that are changing with time i.e. calibration data"""
    return devices.get_device_calibration_info(
        device.bookings_db, device.executor_options.backend_name
    )
