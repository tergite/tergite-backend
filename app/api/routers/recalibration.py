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

from ...libs.queues.dtos import QueueContext
from ...services.scheduler import QueuePool, RecalibrationInfo, get_queue_pool
from ...utils.messages import GeneralMessage
from ..dependencies import get_queue_context_if_online, get_verified_mss_admin_user_id

router = APIRouter()


@router.post(
    "/recalibration/init", dependencies=[Depends(get_verified_mss_admin_user_id)]
)
async def initialize_recalibration(
    context: QueueContext = Depends(get_queue_context_if_online),
    queue_pool: QueuePool = Depends(get_queue_pool),
    start_timestamp: Optional[datetime] = Query(None),
    interval: Optional[float] = Query(None),
) -> GeneralMessage:
    """Initializes recalibration

    Only MSS admin users can initialize recalibration

    Args:
        context: the queue context for the recalibration
        queue_pool: the collection of queues to run the recalibration on
        start_timestamp: the start time of the recalibration; default = None i.e. straight away
        interval: the recalibration interval; default = None i.e. no repeat

    Returns:
        the status of the recalibration
    """
    return scheduler.init_recalibration(
        context, queues=queue_pool, start_timestamp=start_timestamp, interval=interval
    )


@router.get(
    "/recalibration/info", dependencies=[Depends(get_verified_mss_admin_user_id)]
)
async def get_recalibration_info(
    queue_pool: QueuePool = Depends(get_queue_pool),
) -> RecalibrationInfo:
    """Gets the current information about the recalibration

    Only MSS admin users can get recalibration info

    Args:
        queue_pool: the collection of queues to run the recalibration on

    Returns:
        the information about the recalibration
    """
    return scheduler.get_recalibration_info(queues=queue_pool)


@router.post(
    "/recalibration/cancel", dependencies=[Depends(get_verified_mss_admin_user_id)]
)
async def cancel_recalibration(
    context: QueueContext = Depends(get_queue_context_if_online),
    queue_pool: QueuePool = Depends(get_queue_pool),
    ignore_errors: bool = Query(False),
) -> GeneralMessage:
    """Cancels recalibration on this device

    Only MSS admin users can initialize recalibration

    Args:
        context: the queue context for the recalibration
        queue_pool: the collection of queues to run the recalibration on
        ignore_errors: whether to ignore errors during recalibration

    Returns:
        general information on the status of the request
    """
    scheduler.stop_recalibration(
        context, queues=queue_pool, ignore_errors=ignore_errors
    )
    return {"status": "success"}
