# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2023
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
"""Module containing all the queues relevant for this application"""

from typing import Self

from redis import Redis
from rq import Queue as RqQueue

import settings

from ...utils.queues.types import RunnerQueue, StaticQueue
from .tasks import (
    postprocess,
    postprocessing_failure_callback,
    postprocessing_success_callback,
    preprocess,
)


class QueuePool:
    """A collection of Queues

    Attributes:
        preprocessing: the queue for processing before the job is executed
        normal_execution: the queue for normal job execution
        booked_execution: the queue for executing jobs in a booking
        waitlist: the non-running queue (StaticQueue) that is just for waiting
        general: the queue for running any other kind of task
    """

    def __init__(self, prefix: str, connection: "Redis", is_async: bool = True):
        """
        Args:
            prefix: the prefix for the names of the expected queues
            connection: the connection to Redis
            is_async: whether to dispatch the enqueued tasks in other workers
        """
        self._connection = connection
        self._is_async = is_async

        # runner queues
        self.preprocessing = get_preprocessing_queue(
            prefix, connection=connection, is_async=is_async
        )
        self.normal_execution = get_normal_execution_queue(
            prefix, connection=connection, is_async=is_async
        )
        self.booked_execution = get_booked_execution_queue(
            prefix, connection=connection, is_async=is_async
        )
        self.general = get_general_queue(
            prefix, connection=connection, is_async=is_async
        )
        self.postprocessing = get_postprocessing_queue(
            prefix, connection=connection, is_async=is_async
        )

        # static queues
        self.waitlist = get_waitlist(prefix, connection=connection)

    @classmethod
    def from_settings(cls) -> Self:
        """Constructs a queue pool from the settings"""
        return cls(
            prefix=settings.QUEUE_PREFIX,
            connection=Redis.from_url(settings.RQ_REDIS_URL),
            is_async=settings.IS_ASYNC,
        )


def get_waitlist(prefix: str, connection: Redis) -> StaticQueue:
    """Gets the waitlist where jobs that are yet to execute are temporarily held

    Args:
        prefix: the prefix to add to the name of the waitlist
        connection: the redis connection instance where the queue is hosted

    Returns:
        a StaticQueue instance to be used in waitlisting
    """
    return StaticQueue(f"{prefix}_waitlist", connection)


def get_preprocessing_queue(
    prefix: str, connection: Redis, is_async: bool = True, **kwargs
) -> RunnerQueue:
    """Gets the queue for preprocessing

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        is_async: whether the actual queue should be created or
            a dummy one running in the same process to use for testing
        kwargs: other options to pass to the Queue e.g.
            - default_timeout: Optional[int] = None,
            - is_async: bool = True,
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - success_callback: the callback to run on successful execution of the job
            - failure_callback: the callback to run on failed execution of the job
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in preprocessing
    """
    kwargs["job_executor_func"] = preprocess
    return RunnerQueue(
        f"{prefix}_preprocessing", connection, is_async=is_async, **kwargs
    )


def get_normal_execution_queue(
    prefix: str, connection: Redis, is_async: bool = True, **kwargs
) -> RunnerQueue:
    """Gets the queue for executing on the normal queue

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        is_async: whether the actual queue should be created or
            a dummy one running in the same process to use for testing
        kwargs: other options to pass to the Queue e.g.
            - default_timeout: Optional[int] = None,
            - is_async: bool = True,
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - success_callback: the callback to run on successful execution of the job
            - failure_callback: the callback to run on failed execution of the job
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in normal execution
    """
    kwargs["job_executor_func"] = postprocess
    return RunnerQueue(
        f"{prefix}_normal_execution", connection, is_async=is_async, **kwargs
    )


def get_booked_execution_queue(
    prefix: str, connection: Redis, is_async: bool = True, **kwargs
) -> RunnerQueue:
    """Gets the queue for executing on the booked execution queue

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        is_async: whether the actual queue should be created or
            a dummy one running in the same process to use for testing
        kwargs: other options to pass to the Queue e.g.
            - default_timeout: Optional[int] = None,
            - is_async: bool = True,
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - success_callback: the callback to run on successful execution of the job
            - failure_callback: the callback to run on failed execution of the job
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in booked execution
    """
    kwargs["job_executor_func"] = postprocess
    return RunnerQueue(
        f"{prefix}_booked_execution", connection, is_async=is_async, **kwargs
    )


def get_general_queue(
    prefix: str, connection: Redis, is_async: bool = True, **kwargs
) -> RqQueue:
    """Gets the queue for running general tasks

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        is_async: whether the actual queue should be created or
            a dummy one running in the same process to use for testing
        kwargs: other options to pass to the Queue e.g.
            - default_timeout: Optional[int] = None,
            - is_async: bool = True,
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,

    Returns:
        a Queue instance for general tasks
    """
    return RqQueue(f"{prefix}_general", connection, is_async=is_async, **kwargs)


def get_postprocessing_queue(
    prefix: str, connection: Redis, is_async: bool = True, **kwargs
) -> RunnerQueue:
    """Gets the queue for postprocessing

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        is_async: whether the actual queue should be created or
            a dummy one running in the same process to use for testing
        kwargs: other options to pass to the Queue e.g.
            - default_timeout: Optional[int] = None,
            - is_async: bool = True,
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in preprocessing
    """
    kwargs["job_executor_func"] = postprocess
    kwargs["success_callback"] = postprocessing_success_callback
    kwargs["failure_callback"] = postprocessing_failure_callback
    return RunnerQueue(
        f"{prefix}_postprocessing", connection, is_async=is_async, **kwargs
    )
