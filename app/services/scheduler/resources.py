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
"""What one device runs on, got once per process from its settings"""
from dataclasses import dataclass
from typing import Self

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from filelock import FileLock
from redis import Redis
from sqlalchemy import Engine

from ...libs.queues.dtos import ExecutorOptions, Job
from ...utils.config import DeviceSettings
from ...utils.redis import get_redis_connection
from ...utils.redis_store import Collection
from ..booking import get_bookings_sql_engine
from ..external.mss import (
    MssClient,
    get_mss_client,
    get_mss_public_key,
    get_private_key,
)
from .queues import QueuePool, get_queue_pool
from .store import get_jobs_store
from .utils import get_executor_options


@dataclass(frozen=True)
class DeviceResources:
    """What one device runs on: its settings, and the connections, stores, queues, MSS
    client and keys got from them"""

    settings: DeviceSettings
    redis: Redis
    bookings_db: Engine
    jobs_store: Collection[Job]
    queues: QueuePool
    mss_client: MssClient
    executor_options: ExecutorOptions
    mss_public_key: RSAPublicKey
    private_key: RSAPrivateKey
    # held while the device's quantum executor is in use
    executor_lock: FileLock

    @classmethod
    def from_settings(cls, device_settings: DeviceSettings) -> Self:
        """Gets the resources of the device of these settings, which must be the current
        device's, as the getters cache them by the current device"""
        return cls(
            settings=device_settings,
            redis=get_redis_connection(device_settings.rq_redis_url),
            bookings_db=get_bookings_sql_engine(device_settings.booking_db_url),
            jobs_store=get_jobs_store(device_settings.rq_redis_url),
            queues=get_queue_pool(),
            mss_client=get_mss_client(),
            executor_options=get_executor_options(),
            mss_public_key=get_mss_public_key(device_settings.mss_public_key_path),
            private_key=get_private_key(
                device_settings.private_key_file, device_settings.private_key_password
            ),
            executor_lock=FileLock(
                device_settings.executor_data_dir / ".quantum-executor.lock"
            ),
        )
