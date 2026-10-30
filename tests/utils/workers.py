# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2024
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Workers that run a device's heavy and light work in the tests' own process"""
import dataclasses
from datetime import datetime, timedelta
from typing import Any, Generator, Optional

import pytest
import time_machine

import settings
from api.worker import preload_worker_globals
from services.scheduler import close_device_resources
from services.scheduler.dispatcher import Dispatcher, get_next_change_at
from tests.utils.env import (
    TEST_DEFAULT_PREFIX,
    TEST_DEFAULT_PREFIX_SIM_1Q,
    TEST_DEFAULT_PREFIX_SIM_2Q,
    TEST_MAX_WORKER_WAIT_TIME,
)


class TestWorker(Dispatcher):
    """The dispatcher, running a device's heavy and light work until nothing is due"""

    __test__ = False
    should_wait_while_offline = False


@pytest.fixture
def quantify_worker() -> Generator[TestWorker, Any, None]:
    """The worker of the quantify device"""
    yield from _get_device_worker(TEST_DEFAULT_PREFIX)


@pytest.fixture
def qiskit_1q_worker() -> Generator[TestWorker, Any, None]:
    """The worker of the 1-qubit simulator"""
    yield from _get_device_worker(TEST_DEFAULT_PREFIX_SIM_1Q)


@pytest.fixture
def qiskit_2q_worker() -> Generator[TestWorker, Any, None]:
    """The worker of the 2-qubit simulator"""
    yield from _get_device_worker(TEST_DEFAULT_PREFIX_SIM_2Q)


def wait_on_worker(
    worker: TestWorker,
    max_wait_time: int = TEST_MAX_WORKER_WAIT_TIME,
    clock: Optional[time_machine.Traveller] = None,
) -> None:
    """Runs the worker until nothing is due, moving the fake clock, if given, on to the next
    change in the bookings or recalibrations while work waits for one

    Raises:
        TimeoutError: Worker took longer than {max_wait_time} seconds
    """
    deadline = datetime.now() + timedelta(seconds=max_wait_time)
    while datetime.now() < deadline:
        worker.run(is_burst=True)
        if clock is None:
            return
        change_times = [
            get_next_change_at(device) for device in worker.device_resources.values()
        ]
        next_change_at = min(filter(None, change_times), default=None)
        if next_change_at is None:
            return
        clock.move_to(next_change_at)
    raise TimeoutError(f"Worker took longer than {max_wait_time} seconds")


def set_worker_executor(worker: TestWorker, executor: Any) -> None:
    """Makes the worker's devices run their jobs on the given executor, closing theirs"""
    for device, resources in worker.device_resources.items():
        if resources.executor is not None:
            resources.executor.close()
        worker.device_resources[device] = dataclasses.replace(
            resources, executor=executor
        )


def _get_device_worker(prefix: str) -> Generator[TestWorker, Any, None]:
    device = preload_worker_globals(
        settings.get_device_settings(prefix), should_load_executor=True
    )
    worker = TestWorker({prefix: device}, should_run_heavy=True, should_run_light=True)
    yield worker
    for resources in worker.device_resources.values():
        close_device_resources(resources)
