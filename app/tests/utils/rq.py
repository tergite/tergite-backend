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
from typing import List, Union

from rq import Queue, SimpleWorker, Worker
from rq.timeouts import TimerDeathPenalty

from app.api.worker import preload_worker_globals
from app.services.scheduler.queues import QueuePool


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
