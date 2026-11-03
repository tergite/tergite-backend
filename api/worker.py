# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2023
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Loads what a worker runs its devices' tasks with"""
import dataclasses
from importlib import import_module

from filelock import FileLock

from libs.device_parameters import DeviceCalibration
from services.devices import save_all_device_params
from services.scheduler import (
    DeviceResources,
    fail_abandoned_recalibrations,
    get_executor,
)
from utils.config import DeviceSettings, create_device_folders
from utils.sql_db import migrate_sql_db


def preload_worker_globals(
    device_settings: DeviceSettings, should_load_executor: bool
) -> DeviceResources:
    """Initializes the device's resources and other globals, so that the tasks find them loaded

    Args:
        device_settings: the settings of the device the worker serves
        should_load_executor: whether to also load the executor, as a heavy worker does

    Returns:
        the device's resources
    """
    create_device_folders(device_settings)
    # as the API may not have yet, under the lock it migrates under
    with FileLock(device_settings.start_up_lock_file):
        migrate_sql_db(device_settings.booking_db_url)
    # so that the tasks' first run need not import them
    import_module("services.scheduler.tasks")

    device = DeviceResources.from_settings(device_settings)
    if should_load_executor:
        # a heavy worker alone recalibrates, so one starting finds any left by the last
        fail_abandoned_recalibrations(device)
        executor = get_executor(device.executor_options)
        # published here alone, as the executor may refine the backend config, e.g. the
        # simulators train their discriminators, which the API, building none, has not
        executor_options = dataclasses.replace(
            device.executor_options, backend_config=executor.backend_config
        )
        device = dataclasses.replace(
            device, executor=executor, executor_options=executor_options
        )
        backend_config = executor_options.backend_config
        save_all_device_params(
            db_engine=device.bookings_db,
            backend_config=backend_config,
            calibration=executor.add_pulse_calibration(
                DeviceCalibration.from_config(backend_config)
            ),
            mss_client=device.mss_client,
        )
    return device
