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
import logging
import sys

from rq import Queue, Worker
from rq.job import Job

import settings


class PreloadedRqWorker(Worker):
    """RQ Worker that has important libraries, connections etc. preloaded, for each
    device whose queues it runs, and runs each job as the device of its queue."""

    should_preload_executor = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for device in {_get_device_of_queue(queue.name) for queue in self.queues}:
            with settings.using_device(device):
                preload_worker_globals(
                    should_load_executor=self.should_preload_executor
                )

    def perform_job(self, job: Job, queue: Queue) -> bool:
        with settings.using_device(_get_device_of_queue(queue.name)):
            return super().perform_job(job, queue)


class LightRqWorker(PreloadedRqWorker):
    """Preloads all but the quantum executor, for queues whose tasks never run it"""

    should_preload_executor = False


class LoggingRqWorker(PreloadedRqWorker):
    """A special RQ worker that logs its messages for ease of tracking"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

        root = logging.getLogger()
        # avoid duplicate handlers if RQ restarts a child, etc.
        if not root.handlers:
            h = logging.StreamHandler(sys.stdout)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
            )
            h.setFormatter(fmt)
            root.addHandler(h)
        root.setLevel(level)


def preload_worker_globals(should_load_executor: bool) -> None:
    """Initializes connections and other globals, so that the jobs, forked or run in
    process, find them cached"""
    from app.libs.device_parameters import save_all_device_params
    from app.libs.queues.dtos import get_queue_context
    from app.services.booking.store import get_bookings_sql_engine
    from app.services.external.mss.service import get_mss_client
    from app.services.scheduler import tasks  # noqa: F401, so jobs don't import it
    from app.services.scheduler.queues import get_queue_pool
    from app.services.scheduler.store import get_jobs_store
    from app.services.scheduler.utils import get_executor_and_options
    from app.utils.redis import get_redis_connection

    redis_connection = get_redis_connection()
    mss_client = get_mss_client()
    if should_load_executor:
        _, executor_options = get_executor_and_options()
        # the executor may refine the backend config, e.g. the simulators train their
        # discriminators, which the API, building no executor, cannot publish
        save_all_device_params(
            redis=redis_connection,
            backend_config=executor_options.backend_config,
            mss_client=mss_client,
        )
    get_queue_context()
    get_queue_pool()
    get_jobs_store()
    get_bookings_sql_engine()


def _get_device_of_queue(queue_name: str) -> str:
    """Gets the device whose queue this is, its name prefixing the queue's"""
    devices = [
        device
        for device in settings.get_device_names()
        if queue_name.startswith(f"{device}_")
    ]
    # the longest, as one device's name may prefix another's, e.g. sim and sim_2q
    return max(devices, key=len, default=settings.get_device_names()[0])
