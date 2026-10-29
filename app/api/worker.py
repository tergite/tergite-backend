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

"""A worker file that is preloaded with the heavy libraries etc"""
import time
from importlib import import_module
from typing import Any, Optional

from rq import Queue, Worker
from rq.job import Job
from rq.worker import StopRequested

import settings

from ..services.devices import is_offline, save_all_device_params
from ..services.scheduler import (
    DeviceResources,
    get_executor_and_options,
)
from ..utils.config import create_device_folders


class DeviceJob(Job):
    """An RQ job whose function is passed, before its own arguments, the resources of the
    device whose queue it is on, which the worker gives it"""

    device_resources: Optional[DeviceResources] = None

    def _execute(self) -> Any:
        return self.func(self.device_resources, *self.args, **self.kwargs)


class PreloadedRqWorker(Worker):
    """RQ Worker that has important libraries, connections etc. preloaded, for each
    device whose queues it runs, and runs each job as the device of its queue."""

    should_preload_executor = True
    # a heavy worker runs one device, which switching off stops
    should_wait_while_offline = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, job_class=DeviceJob, **kwargs)
        self.device_resources: dict[str, DeviceResources] = {}
        for device in {_get_device_of_queue(queue.name) for queue in self.queues}:
            with settings.using_device(device):
                self.device_resources[device] = preload_worker_globals(
                    should_load_executor=self.should_preload_executor
                )

    def execute_job(self, job: DeviceJob, queue: Queue) -> None:
        # before a forking worker forks, as the work horse's death runs the callbacks here
        job.device_resources = self.device_resources[_get_device_of_queue(queue.name)]
        super().execute_job(job, queue)

    def perform_job(self, job: DeviceJob, queue: Queue) -> bool:
        with settings.using_device(_get_device_of_queue(queue.name)):
            return super().perform_job(job, queue)

    def check_for_suspension(self, is_burst: bool) -> None:
        """Also waits, as rq's own suspension does, while the worker's device is switched off"""
        super().check_for_suspension(is_burst)
        device = _get_device_of_queue(self.queues[0].name)
        while (
            self.should_wait_while_offline
            and not self._stop_requested
            and is_offline(self.device_resources[device].redis, device)
        ):
            if is_burst:
                raise StopRequested
            self.heartbeat()
            time.sleep(1)


class LightRqWorker(PreloadedRqWorker):
    """Preloads all but the quantum executor, for queues whose tasks never run it"""

    should_preload_executor = False
    should_wait_while_offline = False


def preload_worker_globals(should_load_executor: bool) -> DeviceResources:
    """Initializes the current device's resources and other globals, so that the jobs,
    forked or run in process, find them cached

    Returns:
        the current device's resources
    """
    create_device_folders(settings.get_device_settings())
    # so that the jobs need not import it
    import_module("app.services.scheduler.tasks")

    device = DeviceResources.from_settings(settings.get_device_settings())
    if should_load_executor:
        _, executor_options = get_executor_and_options()
        # the executor may refine the backend config, e.g. the simulators train their
        # discriminators, which the API, building no executor, cannot publish
        save_all_device_params(
            redis=device.redis,
            backend_config=executor_options.backend_config,
            mss_client=device.mss_client,
        )
    return device


def _get_device_of_queue(queue_name: str) -> str:
    """Gets the device whose queue this is, its name prefixing the queue's"""
    devices = [
        device
        for device in settings.get_device_names()
        if queue_name.startswith(f"{device}_")
    ]
    # the longest, as one device's name may prefix another's, e.g. sim and sim_2q
    return max(devices, key=len, default=settings.get_device_names()[0])
