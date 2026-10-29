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
from redis import Redis

from ...libs import device_parameters as props_lib
from ...utils.redis import get_redis_connection
from ..dependencies import get_backend_name, get_verified_mss_user_id

router = APIRouter()


@router.get("/static-properties", dependencies=[Depends(get_verified_mss_user_id)])
async def get_static_properties(
    redis: Redis = Depends(get_redis_connection),
    backend_name: str = Depends(get_backend_name),
):
    """Retrieves the device properties that are not changing"""
    return props_lib.get_device_info(redis, backend_name)


@router.get("/dynamic-properties", dependencies=[Depends(get_verified_mss_user_id)])
async def get_dynamic_properties(
    redis: Redis = Depends(get_redis_connection),
    backend_name: str = Depends(get_backend_name),
):
    """Retrieves the device properties that are changing with time i.e. calibration data"""
    return props_lib.get_device_calibration_info(redis, backend_name)
