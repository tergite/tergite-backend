# This code is part of Tergite
#
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
"""Module containing the types of queues"""

import math
from contextlib import suppress
from types import TracebackType
from typing import Any, Callable, Generator, Optional, Type, Union

from pydantic import ValidationError
from redis import Redis
from rq import Callback, Queue, get_current_job
from rq.job import Job as RqJob
from rq.results import Result as RqResult
from rq.serializers import Serializer
from rq.timeouts import BaseDeathPenalty, UnixSignalDeathPenalty

from ..funcs import get_function_import_path, import_func, noop
from ..rq import cancel_rq_job
from .dtos import Job, StorageID


class StaticQueue:
    """A Queue that is not associated with any runner

    It thus just keeps items without running them
    """

    def __init__(self, name: str, connection: "Redis"):
        """
        Args:
            name: the name of the queue
            connection: the connection to Redis
        """
        self.name = name
        self._database = f"backing_map_{name}"
        self._metadata = f"metadata_{name}"
        self._duration_key = "total_duration"
        self._current_job = "current_job"
        self._connection = connection

    @property
    def total_duration(self) -> float:
        """the total duration in seconds for all queued tasks"""
        try:
            value = self._connection.hget(self._metadata, self._duration_key)
            return float(value)
        except TypeError:
            return 0

    @property
    def current_job(self) -> Optional[Job]:
        """The job that is currently being run or was last run"""
        try:
            payload = self._connection.hget(self._metadata, self._current_job)
            return Job.model_validate_json(payload)
        except ValidationError:
            return None

    def add(self, job: Job):
        """Adds the item to the queue

        Args:
            job: the item to add to the queue
        """
        payload = job.model_dump_json()

        pipe = self._connection.pipeline()
        pipe.hset(self._database, job.storage_id, payload)
        pipe.rpush(self.name, job.storage_id)
        pipe.hincrbyfloat(
            self._metadata, self._duration_key, job.estimated_duration or 0
        )

        pipe.execute()

    def next(self) -> Optional[Job]:
        """Gets the next item on the queue

        Returns:
            the str that is next on the queue or None if there is no item
        """
        try:
            storage_id = self._connection.lpop(self.name)
            storage_id = StorageID(_to_str(storage_id))
            job_duration = storage_id.duration

            pipe = self._connection.pipeline()

            pipe.hincrbyfloat(self._metadata, self._duration_key, -job_duration)
            pipe.hget(self._database, storage_id)
            pipe.hdel(self._database, storage_id)
            pipeline_results = pipe.execute()

            payload = pipeline_results[1]
            job = Job.model_validate_json(payload)
            self._connection.hset(self._metadata, self._current_job, payload)
            return job
        except ValidationError:
            return None

    def clear(self):
        """Removes all items on the queue"""
        # network request 1
        self._connection.delete(self._database, self.name, self._metadata)

    def pop_job(self, storage_id: StorageID) -> Job:
        """Removes a job out of this queue and returns it

        Args:
            storage_id: the storage id of the job

        Returns:
            the job

        Raises:
            ValidationError: job is of invalid format or does not exist
        """
        job_duration = storage_id.duration
        pipe = self._connection.pipeline()

        pipe.lrem(self.name, 1, storage_id)
        pipe.hget(self._database, storage_id)
        pipe.hdel(self._database, storage_id)
        pipe.hincrbyfloat(self._metadata, self._duration_key, -job_duration)

        pipeline_results = pipe.execute()
        job_json = pipeline_results[1]

        return Job.model_validate_json(job_json)

    def pop_first(
        self, filter_func: Callable[[StorageID, ...], bool], *args, **kwargs
    ) -> Optional[Job]:
        """Pops the first job that fulfills the given filter function

        Args:
            filter_func: the function that receives the job storage id and returns true if condition is fulfilled, else false
            args: extra arguments to pass to the filter function apart from the storage_id
            kwargs: extra key-word arguments to pass to the filter function

        Returns:
            the job if one is found fulfilling the condition or None
        """
        num_of_entries = self._connection.llen(self.name)
        batch_size = 100
        num_of_batches = math.ceil(num_of_entries / batch_size)

        batch = 0
        while batch < num_of_batches:
            start = batch * batch_size
            stop = start + batch_size - 1

            entries = self._connection.lrange(self.name, start, stop)
            for idx, storage_id in enumerate(entries):
                storage_id = StorageID(_to_str(storage_id))

                if filter_func(storage_id, *args, **kwargs):
                    return self.pop_job(storage_id)

            # move to next batch
            batch += 1
        return None

    def pop_many(
        self,
        max_total_duration: Optional[float] = None,
    ) -> Generator[Job, None, None]:
        """Pops jobs in FIFO order given a few limitations

        Args:
            max_total_duration: the total seconds the collection of jobs should not exceed; default None

        Returns:
            the generator of jobs
        """
        if max_total_duration is None:
            max_total_duration = float("inf")

        num_of_entries = self._connection.llen(self.name)
        batch_size = 100
        num_of_batches = math.ceil(num_of_entries / batch_size)
        available_duration = max_total_duration

        batch = 0
        while batch < num_of_batches and available_duration > 0:
            start = batch * batch_size
            stop = start + batch_size - 1

            entries = self._connection.lrange(self.name, start, stop)
            for idx, storage_id in enumerate(entries):
                storage_id = StorageID(_to_str(storage_id))
                duration = storage_id.duration

                if duration > available_duration:
                    # move to the next entry if current duration
                    # is too big for available duration
                    continue

                job = self.pop_job(storage_id)
                yield job

                # update the available duration
                available_duration -= duration

            # move to next batch
            batch += 1


class RunnerQueue(Queue):
    """The queue that can run jobs"""

    def __init__(
        self,
        name: str = "default",
        connection: Redis | None = None,
        default_timeout: int | None = None,
        is_async: bool = True,
        job_class: str | Type[RqJob] | None = None,
        serializer: Serializer | str | None = None,
        death_penalty_class: Type[BaseDeathPenalty] | None = UnixSignalDeathPenalty,
        job_executor_func: Union[Callable[[Job, ...], Any], str] = noop,
        success_callback: Callable[[RqJob, Redis, RqResult, ...], Any] = noop,
        failure_callback: Callable[
            [RqJob, Redis, Type[Exception], Exception, TracebackType, ...], Any
        ] = noop,
        stopped_callback: Callable[[RqJob, Redis, ...], Any] = noop,
        **kwargs: Any,
    ):
        """Initializes a Queue object.

        Args:
            name: The queue name. Defaults to 'default'.
            default_timeout: Queue's default timeout. Defaults to None.
            connection: Redis connection. Defaults to None.
            is_async: Whether jobs should run "async" (using the worker).
                If `is_async` is false, jobs will run on the same process from where it was called. Defaults to True.
            job_class: Job class or a string referencing the Job class path.
                Defaults to None.
            serializer: Serializer. Defaults to None.
            death_penalty_class: Job class or a string referencing the Job class path.
                Defaults to UnixSignalDeathPenalty.
            job_executor_func: function to execute the job. It can be the import path or the function itself.
                Defaults to noop.
            success_callback: the callback to run on successful execution of the job
            failure_callback: the callback to run on failed execution of the job
            stopped_callback: the callback to run when a job is stopped before it completes
        """
        super().__init__(
            name=name,
            connection=connection,
            default_timeout=default_timeout,
            is_async=is_async,
            job_class=job_class,
            serializer=serializer,
            death_penalty_class=death_penalty_class,
            **kwargs,
        )
        if callable(job_executor_func):
            job_executor_func = get_function_import_path(job_executor_func)

        self._static_queue = StaticQueue(f"__static_{name}", connection=connection)
        self._job_executor_func = job_executor_func
        self._success_callback = _to_callback(success_callback)
        self._failure_callback = _to_callback(failure_callback)
        self._stopped_callback = _to_callback(stopped_callback)
        self._current_job: Optional[Job] = None

    @property
    def total_duration(self) -> float:
        """the total duration of all pending jobs plus the time left to the end of the current job"""
        try:
            return (
                self._static_queue.total_duration
                + self._static_queue.current_job.current_eta
            )
        except (AttributeError, TypeError):
            return self._static_queue.total_duration

    def enqueue(self, job: Job, *args, **kwargs) -> RqJob:
        """Creates a rq job to represent the delayed function call and enqueues it.
        Receives the same parameters accepted by the `enqueue_call` method except for 'f'.

        Args:
            job: The job to pass to this queue
            args: function args
            kwargs: function kwargs

        Returns:
            rq_job (rq.job.Job): The created rq.job.Job
        """
        self._static_queue.add(job)
        kwargs = {
            "on_success": self._success_callback,
            "on_failure": self._failure_callback,
            "on_stopped": self._stopped_callback,
            **kwargs,
            "job_id": self.get_rq_job_id(job.job_id),
        }
        return super().enqueue(
            _run_next_job,
            self._static_queue.name,
            self._job_executor_func,
            *args,
            **kwargs,
        )

    def get_rq_job_id(self, job_id: str) -> str:
        """Generates the rq job id for the job of given job_id

        Args:
            job_id: the unique identifier of the job (not rq job)

        Returns:
            the id of the rq job for the given job_id
        """
        return f"{self.name}_{job_id}"

    def cancel_job(
        self,
        job_id: str,
        storage_id: Optional[StorageID] = None,
        ignore_errors: bool = False,
    ):
        """Cancels the job of the given job_id

        Args:
            job_id: the unique identifier of the job (not rq job)
            storage_id: the identifier of the job in the static queue storage
            ignore_errors: whether to ignore errors; default = False

        Raises:
            rq.exceptions.NoSuchJobError: if the job does not exist
            rq.exceptions.InvalidJobOperationError: if the job has already been cancelled
            rq.exceptions.InvalidJobOperation: if the job has already been cancelled
        """
        if storage_id:
            with suppress(ValidationError):
                # delete the job from the static queue.
                # ignore error if storage id does not exist
                self._static_queue.pop_job(storage_id)

        # cancel the job if it is pending or running
        # or raise an appropriate error
        rq_job_id = self.get_rq_job_id(job_id)
        cancel_rq_job(self.connection, rq_job_id, ignore_errors=ignore_errors)


def _run_next_job(static_queue_name, exec_func_path: str, *args, **kwargs):
    """A wrapper around the execution function to do some book-keeping on next job on the queue

    Args:
        static_queue_name: the name of the static queue
        exec_func_path: the import path of the function that executes this job
        args: function args
        kwargs: function kwargs
    """
    connection = get_current_job().connection
    static_queue = StaticQueue(static_queue_name, connection=connection)
    job = static_queue.next()
    exec_func = import_func(exec_func_path)
    return exec_func(job, *args, **kwargs)


def _to_str(value: Union[str, bytes]) -> str:
    """Gets a string from a value that may be bytes

    Args:
        value: the value that may be bytes

    Returns:
        the string form of that value
    """
    try:
        return value.decode("utf-8")
    except AttributeError:
        return value


def _to_callback(func: Union[str, Callable[..., Any], Callback]) -> Callback:
    """Converts a function or str into an rq callback

    Args:
        func: the function or its import path or its callback object

    Returns:
        the callback with the function
    """
    if isinstance(func, Callback):
        return func
    return Callback(func)
