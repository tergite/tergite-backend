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
"""Module containing the store for the scheduler service"""


from redis import Redis

from ...libs.queues.dtos import Job
from ...utils.config import DeviceSettings
from ...utils.redis_store import Collection


def get_jobs_store(redis: Redis, device_settings: DeviceSettings) -> Collection[Job]:
    """Gets the device's store for the jobs, on the given Redis, which several devices may
    share, as the device's settings say to keep them"""
    return Collection(
        connection=redis,
        schema=Job,
        default_ttl=device_settings.jobs_store_ttl,
        cleanup_interval=device_settings.jobs_store_clean_interval,
        name=f"{device_settings.default_prefix}:jobs",
    )
