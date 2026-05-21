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
import sys
from typing import List

from rq import Queue, SimpleWorker
from rq.timeouts import TimerDeathPenalty

from app.services.scheduler.queues import QueuePool


class WindowsSimpleWorker(SimpleWorker):
    death_penalty_class = TimerDeathPenalty


class PseudoSimpleWorker(SimpleWorker):
    death_penalty_class = TimerDeathPenalty

    def work(self, burst: bool = False, **kwargs) -> bool:
        return True


def get_rq_pool_worker(queue_pool: QueuePool) -> SimpleWorker:
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


def get_rq_worker(queues: List[Queue], is_async: bool = True) -> SimpleWorker:
    """Returns an rq worker to run a set of queues

    They must share the same redis connection

    Args:
        queues: the set of queue to run
        is_async: whether the jobs should be run in separate processed
    """
    connection = queues[0].connection
    if not is_async:
        return PseudoSimpleWorker(queues=queues, connection=connection)

    if sys.platform.startswith("win32"):
        return WindowsSimpleWorker(queues=queues, connection=connection)

    return SimpleWorker(queues=queues, connection=connection)
