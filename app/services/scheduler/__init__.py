# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Runs the jobs on the device: their queues, the bookings' hold on them, and recalibration"""
from .dtos import JobFile, RecalibrationInfo
from .queues import QueuePool
from .resources import DeviceResources, close_device_resources
from .service import (
    cancel_booking,
    cancel_job,
    delete_job,
    delete_user_profile,
    get_job,
    get_many_jobs,
    get_recalibration_info,
    init_recalibration,
    stop_recalibration,
    submit_booking,
    submit_job_file,
)
from .store import get_jobs_store
from .utils import (
    clear_quantum_executor,
    get_executor_and_options,
    get_executor_options,
)

__all__ = [
    "DeviceResources",
    "JobFile",
    "QueuePool",
    "RecalibrationInfo",
    "cancel_booking",
    "cancel_job",
    "clear_quantum_executor",
    "close_device_resources",
    "delete_job",
    "delete_user_profile",
    "get_executor_and_options",
    "get_executor_options",
    "get_job",
    "get_jobs_store",
    "get_many_jobs",
    "get_recalibration_info",
    "init_recalibration",
    "stop_recalibration",
    "submit_booking",
    "submit_job_file",
]
