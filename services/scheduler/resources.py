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
from typing import TYPE_CHECKING, Optional, Self

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from filelock import FileLock
from sqlalchemy import Engine

from libs.queues.dtos import ExecutorOptions
from utils.config import DeviceSettings
from utils.logging import err_logger
from utils.sql_db import get_sql_engine

from ..external.mss import MssClient, get_mss_public_key, get_private_key
from .store import JobsStore
from .utils import get_executor_options

if TYPE_CHECKING:
    from libs.quantum_executor.base.executor import QuantumExecutor


@dataclass(frozen=True)
class DeviceResources:
    """What one device runs on: its settings, and the connections, stores, MSS client and
    keys got from them"""

    settings: DeviceSettings
    bookings_db: Engine
    jobs_store: JobsStore
    mss_client: MssClient
    executor_options: ExecutorOptions
    mss_public_key: RSAPublicKey
    private_key: RSAPrivateKey
    # held while the device's quantum executor is in use
    executor_lock: FileLock
    # the heavy worker's only, which runs the jobs on the device
    executor: Optional["QuantumExecutor"] = None

    @classmethod
    def from_settings(cls, device_settings: DeviceSettings) -> Self:
        """Gets the resources of the device of these settings"""
        private_key = get_private_key(
            device_settings.private_key_file, device_settings.private_key_password
        )
        sql_db = get_sql_engine(device_settings.booking_db_url)
        return cls(
            settings=device_settings,
            bookings_db=sql_db,
            jobs_store=JobsStore.from_settings(sql_db, device_settings),
            mss_client=MssClient.from_settings(device_settings, private_key),
            executor_options=get_executor_options(device_settings),
            mss_public_key=get_mss_public_key(device_settings.mss_public_key_path),
            private_key=private_key,
            executor_lock=FileLock(
                device_settings.executor_data_dir / ".quantum-executor.lock"
            ),
        )


def close_device_resources(device: DeviceResources) -> None:
    """Closes the connections and the executor the device's resources hold open, logging
    any MSS or executor failure"""
    try:
        device.mss_client.close()
    except Exception as exp:
        err_logger.warning(f"error closing the MSS client: {exp}")
    device.bookings_db.dispose()
    if device.executor is not None:
        try:
            device.executor.close()
        except Exception as exp:
            err_logger.warning(f"error closing the quantum executor: {exp}")
