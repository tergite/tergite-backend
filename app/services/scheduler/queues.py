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
from functools import cached_property
from typing import Self

from redis import Redis
from rq import Queue as RqQueue

from ...libs.queues.types import RunnerQueue, StaticQueue
from ...utils.config import DeviceSettings

# the tasks go by import path, as importing them loads the executor's heavy libraries
_TASKS_MODULE = f"{__package__}.tasks"


class QueuePool:
    """A collection of Queues

    Attributes:
        preprocessing: the queue for processing before the job is executed
        normal_execution: the queue for normal job execution
        booked_execution: the queue for executing jobs in a booking
        waitlist: the non-running queue (StaticQueue) that is just for waiting
        general: the queue for running any other kind of task
    """

    def __init__(
        self,
        prefix: str,
        connection: "Redis",
        preprocessing_timeout: int,
        execution_timeout: int,
        postprocessing_timeout: int,
        general_queue_timeout: int,
        recalibration_timeout: int,
    ):
        """
        Args:
            prefix: the prefix for the names of the expected queues
            connection: the connection to Redis
            preprocessing_timeout: maximum time jobs spend preprocessing
            execution_timeout: maximum time jobs spend executing
            postprocessing_timeout: maximum time jobs spend postprocessing
            general_queue_timeout: maximum time tasks spend on general queue
            recalibration_timeout: maximum time tasks spend recalibration queue
        """
        self._connection = connection

        # runner queues
        self.preprocessing = get_preprocessing_queue(
            prefix,
            connection=connection,
            default_timeout=preprocessing_timeout,
        )
        self.normal_execution = get_normal_execution_queue(
            prefix,
            connection=connection,
            default_timeout=execution_timeout,
        )
        self.booked_execution = get_booked_execution_queue(
            prefix,
            connection=connection,
            default_timeout=execution_timeout,
        )
        self.general = get_general_queue(
            prefix,
            connection=connection,
            default_timeout=general_queue_timeout,
        )
        self.postprocessing = get_postprocessing_queue(
            prefix,
            connection=connection,
            default_timeout=postprocessing_timeout,
        )

        self.recalibration: RqQueue = get_recalibration_queue(
            prefix,
            connection=connection,
            default_timeout=recalibration_timeout,
        )

        # static queues
        self.waitlist = get_waitlist(prefix, connection=connection)

    @cached_property
    def queues(self) -> dict[str, StaticQueue | RqQueue | RunnerQueue]:
        """Mapping of the queues in the pool, keyed by name"""
        return {
            self.preprocessing.name: self.preprocessing,
            self.normal_execution.name: self.normal_execution,
            self.booked_execution.name: self.booked_execution,
            self.general.name: self.general,
            self.postprocessing.name: self.postprocessing,
            self.recalibration.name: self.recalibration,
            self.waitlist.name: self.waitlist,
        }

    @classmethod
    def from_settings(cls, device_settings: DeviceSettings, connection: Redis) -> Self:
        """Constructs the device's queue pool, on the given connection"""
        return cls(
            prefix=device_settings.default_prefix,
            connection=connection,
            preprocessing_timeout=device_settings.max_preprocessing_time,
            execution_timeout=device_settings.max_execution_time,
            postprocessing_timeout=device_settings.max_postprocessing_time,
            general_queue_timeout=device_settings.max_general_queue_time,
            recalibration_timeout=device_settings.max_recalibration_queue_time,
        )

    def empty(self):
        """Empties the pool's queues

        Raises:
            ExceptionGroup: if any of the attempts to empty the queues errored out
        """
        errs: list[Exception] = []
        for queue in self.queues.values():
            try:
                queue.empty()
            except Exception as exp:
                errs.append(exp)

        if errs:
            raise ExceptionGroup("Errors emptying the queues", errs)


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
    prefix: str,
    connection: Redis,
    default_timeout: int,
    **kwargs,
) -> RunnerQueue:
    """Gets the queue for preprocessing

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        default_timeout: the default maximum time a job can take on this queue
        kwargs: other options to pass to the Queue e.g.
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - success_callback: the callback to run on successful execution of the job
            - failure_callback: the callback to run on failed execution of the job
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in preprocessing
    """
    kwargs["job_executor_func"] = f"{_TASKS_MODULE}.preprocess"
    return RunnerQueue(
        f"{prefix}_preprocessing",
        connection,
        default_timeout=default_timeout,
        **kwargs,
    )


def get_normal_execution_queue(
    prefix: str,
    connection: Redis,
    default_timeout: int,
    **kwargs,
) -> RunnerQueue:
    """Gets the queue for executing on the normal queue

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        default_timeout: the default maximum time a job can take on this queue
        kwargs: other options to pass to the Queue e.g.
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - success_callback: the callback to run on successful execution of the job
            - failure_callback: the callback to run on failed execution of the job
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in normal execution
    """
    kwargs["job_executor_func"] = f"{_TASKS_MODULE}.execute"
    return RunnerQueue(
        f"{prefix}_normal_execution",
        connection,
        default_timeout=default_timeout,
        **kwargs,
    )


def get_booked_execution_queue(
    prefix: str,
    connection: Redis,
    default_timeout: int,
    **kwargs,
) -> RunnerQueue:
    """Gets the queue for executing on the booked execution queue

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        default_timeout: the default timeout in seconds
        default_timeout: the default maximum time a job can take on this queue
        kwargs: other options to pass to the Queue e.g.
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - success_callback: the callback to run on successful execution of the job
            - failure_callback: the callback to run on failed execution of the job
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in booked execution
    """
    kwargs["job_executor_func"] = f"{_TASKS_MODULE}.execute"
    return RunnerQueue(
        f"{prefix}_booked_execution",
        connection,
        default_timeout=default_timeout,
        **kwargs,
    )


def get_general_queue(
    prefix: str,
    connection: Redis,
    default_timeout: int,
    **kwargs,
) -> RqQueue:
    """Gets the queue for running general tasks

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        default_timeout: the default maximum time a job can take on this queue
        kwargs: other options to pass to the Queue e.g.
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,

    Returns:
        a Queue instance for general tasks
    """
    return RqQueue(
        f"{prefix}_general",
        connection,
        default_timeout=default_timeout,
        **kwargs,
    )


def get_postprocessing_queue(
    prefix: str,
    connection: Redis,
    default_timeout: int,
    **kwargs,
) -> RunnerQueue:
    """Gets the queue for postprocessing

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        default_timeout: the default maximum time a job can take on this queue
        kwargs: other options to pass to the Queue e.g.
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in preprocessing
    """
    kwargs["job_executor_func"] = f"{_TASKS_MODULE}.postprocess"
    kwargs["success_callback"] = f"{_TASKS_MODULE}.postprocessing_success_callback"
    kwargs["failure_callback"] = f"{_TASKS_MODULE}.postprocessing_failure_callback"
    return RunnerQueue(
        f"{prefix}_postprocessing",
        connection,
        default_timeout=default_timeout,
        **kwargs,
    )


def get_recalibration_queue(
    prefix: str,
    connection: Redis,
    default_timeout: int,
    **kwargs,
) -> RqQueue:
    """Gets the queue for recalibration

    Args:
        prefix: the prefix to add to the name of the queue
        connection: the redis connection instance where the queue is hosted
        default_timeout: the default maximum time a job can take on this queue
        kwargs: other options to pass to the Queue e.g.
            - job_class: Optional[Union[str, type['Job']]] = None,
            - serializer: Optional[Union[Serializer, str]] = None,
            - death_penalty_class: Optional[type[BaseDeathPenalty]] = UnixSignalDeathPenalty,
            - stopped_callback: the callback to run when a job is stopped before it completes

    Returns:
        a RunnerQueue instance to be used in preprocessing
    """
    return RqQueue(
        f"{prefix}_recalibration",
        connection,
        default_timeout=default_timeout,
        **kwargs,
    )
