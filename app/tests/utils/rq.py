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

"""Utilities for testing rq workers and queues"""
from datetime import datetime, timedelta, timezone
from typing import (
    List,
    Optional,
    Union,
)

import pytest
import time_machine
from redis import Redis
from rq import Queue
from rq import Queue as RqQueue
from rq import SimpleWorker, Worker
from rq.timeouts import TimerDeathPenalty

from app.api.worker import preload_worker_globals
from app.services.scheduler.queues import QueuePool
from app.tests.utils.env import (
    TEST_DEFAULT_PREFIX,
    TEST_DEFAULT_PREFIX_SIM_1Q,
    TEST_DEFAULT_PREFIX_SIM_2Q,
    TEST_MAX_EXECUTION_TIME,
    TEST_MAX_GENERAL_QUEUE_TIME,
    TEST_MAX_POSTPROCESSING_TIME,
    TEST_MAX_PREPROCESSING_TIME,
    TEST_MAX_RECALIBRATION_QUEUE_TIME,
    TEST_RQ_MAX_QUEUE_WAIT_TIME,
)


class PseudoSimpleWorker(SimpleWorker):
    death_penalty_class = TimerDeathPenalty

    def work(self, burst: bool = False, **kwargs) -> bool:
        return True


class PreloadedSimpleWorker(SimpleWorker):
    """RQ SimpleWorker used in all test environments (no forking)."""

    death_penalty_class = TimerDeathPenalty

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        preload_worker_globals(should_load_executor=True)


def get_rq_pool_worker(queue_pool: QueuePool) -> Union[Worker, SimpleWorker]:
    """Returns an rq worker to run the given queue pool

    Args:
        queue_pool: the QueuePool whose queues are to be run
    """
    queues = [
        queue_pool.general,
        queue_pool.booked_execution,
        queue_pool.normal_execution,
        queue_pool.preprocessing,
        queue_pool.postprocessing,
        queue_pool.recalibration,
    ]
    return get_rq_worker(queues, is_async=queue_pool._is_async)


def get_rq_worker(
    queues: List[Queue], is_async: bool = True
) -> Union[Worker, SimpleWorker]:
    """Returns an rq worker to run a set of queues

    They must share the same redis connection

    Args:
        queues: the set of queues to run
        is_async: whether the jobs should be run in separate processes
    """
    connection = queues[0].connection
    if not is_async:
        return PseudoSimpleWorker(queues=queues, connection=connection)

    return PreloadedSimpleWorker(queues=queues, connection=connection)


@pytest.fixture
def rq_worker(redis_client) -> Union[Worker, SimpleWorker]:
    """The worker of the quantify device's queues"""
    return _get_device_worker(redis_client, prefix=TEST_DEFAULT_PREFIX)


@pytest.fixture
def rq_worker_for_simulator_1q(redis_client) -> Union[Worker, SimpleWorker]:
    """The worker of the 1-qubit simulator's queues"""
    return _get_device_worker(redis_client, prefix=TEST_DEFAULT_PREFIX_SIM_1Q)


@pytest.fixture
def rq_worker_for_simulator_2q(redis_client) -> Union[Worker, SimpleWorker]:
    """The worker of the 2-qubit simulator's queues"""
    return _get_device_worker(redis_client, prefix=TEST_DEFAULT_PREFIX_SIM_2Q)


def get_queue_by_name(worker: SimpleWorker, name: str) -> RqQueue:
    """Gets the rq queue by name

    Args:
        worker: the worker running the queues
        name: the name of the queue

    Returns:
        the rq queue object

    Raises:
        KeyError: if the queue doesn't exist
    """
    try:
        return [v for v in worker.queues if v.name == name][0]
    except IndexError:
        raise KeyError(f"queue '{name}' not found")


def wait_on_rq_worker(
    worker: SimpleWorker,
    max_idle_time: int = TEST_RQ_MAX_QUEUE_WAIT_TIME,
    burst: bool = True,
    max_jobs: Optional[int] = None,
    with_scheduler: bool = False,
    queue: RqQueue = None,
    clock: Optional[time_machine.Traveller] = None,
    **kwargs,
):
    """Waits for the worker to complete its worker

    Args:
        worker: the rq worker to wait on
        max_idle_time: the number of seconds beyond which a timeout is raised
        with_scheduler: whether to run in a scheduler
        queue: the queue to wait on; defaults to the first queue i.e. the general queue
        clock: the fake clock to move on to the next scheduled job when none is queued,
            instead of waiting for it
        kwargs: extra kwargs to pass to the work() method of the worker.
            See the signature of that method

    Raises:
        TimeoutError: Worker took longer than {max_idle_time} seconds
        KeyError: if the queue of given name doesn't exist
    """
    if queue is None:
        queue = worker.queues[0]

    # in order to avoid this from hanging,
    # whenever with_scheduler is True, burst must be True so that it quits
    # immediately after starting the scheduler.
    # With burst=True, the locks are released immediately after running the jobs
    if with_scheduler:
        burst = True

    deadline = datetime.now() + timedelta(seconds=max_idle_time)
    initial_completed_jobs = (
        queue.finished_job_registry.count + queue.failed_job_registry.count
    )
    pending_jobs = queue.scheduled_job_registry.count + len(queue.jobs)
    total_run_jobs = 0

    while pending_jobs > 0 and (max_jobs is None or total_run_jobs < max_jobs):
        now = datetime.now()
        if now > deadline:
            raise TimeoutError(f"Worker took longer than {max_idle_time} seconds")

        worker.work(
            burst=burst,
            with_scheduler=with_scheduler,
            max_jobs=max_jobs,
            **kwargs,
        )

        if clock is not None and len(queue.jobs) == 0:
            registry = queue.scheduled_job_registry
            scheduled_times = [
                registry.get_scheduled_time(job_id) for job_id in registry.get_job_ids()
            ]
            if scheduled_times and min(scheduled_times) > datetime.now(timezone.utc):
                clock.move_to(min(scheduled_times))

        # refresh variables
        pending_jobs = queue.scheduled_job_registry.count + len(queue.jobs)
        current_completed_jobs = (
            queue.finished_job_registry.count + queue.failed_job_registry.count
        )
        total_run_jobs = current_completed_jobs - initial_completed_jobs


def _get_device_worker(redis_client: Redis, prefix: str) -> Union[Worker, SimpleWorker]:
    queue_pool = QueuePool(
        prefix=prefix,
        connection=redis_client,
        is_async=True,
        execution_timeout=TEST_MAX_EXECUTION_TIME,
        preprocessing_timeout=TEST_MAX_PREPROCESSING_TIME,
        postprocessing_timeout=TEST_MAX_POSTPROCESSING_TIME,
        general_queue_timeout=TEST_MAX_GENERAL_QUEUE_TIME,
        recalibration_timeout=TEST_MAX_RECALIBRATION_QUEUE_TIME,
    )
    return get_rq_pool_worker(queue_pool)
