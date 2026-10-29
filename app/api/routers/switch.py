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

from app.services import scheduler

from ...libs.queues.dtos import QueueContext, get_queue_context
from ...utils.api import GeneralMessage
from ..dependencies import (
    get_queue_context_if_online,
    get_verified_mss_admin_user_id,
    get_verified_mss_user_id,
)

router = APIRouter()


# plain def, so FastAPI runs them in a thread: telling MSS blocks, for up to minutes
@router.post("/switch/off", dependencies=[Depends(get_verified_mss_admin_user_id)])
def switch_off(
    context: QueueContext = Depends(get_queue_context_if_online),
) -> GeneralMessage:
    """Switches off this device

    Only MSS admin users can switch on and off this device

    Args:
        context: the queue context for the recalibration

    Returns:
        general information on the status of the request
    """
    scheduler.switch_off(context)
    return {"status": "success"}


@router.post("/switch/on", dependencies=[Depends(get_verified_mss_admin_user_id)])
def switch_on(
    context: QueueContext = Depends(get_queue_context),
) -> GeneralMessage:
    """Switches on this device

    Only MSS admin users can switch on and off this device

    Args:
        context: the queue context for the recalibration

    Returns:
        general information on the status of the request
    """
    scheduler.switch_on(context)
    return {"status": "success"}


@router.get("/switch/status", dependencies=[Depends(get_verified_mss_user_id)])
async def get_switch_status(
    context: QueueContext = Depends(get_queue_context),
):
    """Gets the status of this device

    Args:
        context: the queue context for the recalibration

    Returns:
        general information on the status of the request
    """
    status = "OFF" if scheduler.is_offline(context) else "ON"
    return {"status": status}
