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
import zlib
from typing import Any, Callable, Dict, Generator, Optional, Tuple, Union

from redis import Redis
from rq.serializers import DefaultSerializer, Serializer

from .dtos import Job, StorageID

# KEYS: the queue's list, jobs hash, args-kwargs hash and metadata hash
# ARGV: the storage id, the metadata's duration field and the job's negated duration
_POP_JOB_SCRIPT = """
if redis.call("LREM", KEYS[1], 1, ARGV[1]) == 1 then
    redis.call("HINCRBYFLOAT", KEYS[4], ARGV[2], ARGV[3])
end
local job_json = redis.call("HGET", KEYS[2], ARGV[1])
local args_kwargs = redis.call("HGET", KEYS[3], ARGV[1])
redis.call("HDEL", KEYS[2], ARGV[1])
redis.call("HDEL", KEYS[3], ARGV[1])
return {job_json, args_kwargs}
"""


class StaticQueue:
    """A Queue that is not associated with any runner

    It thus just keeps items without running them
    """

    def __init__(
        self, name: str, connection: "Redis", serializer: Serializer = DefaultSerializer
    ):
        """
        Args:
            name: the name of the queue
            connection: the connection to Redis
            serializer: the class for serializing the args and kwargs of the job
        """
        self.name = name
        self._database = f"backing_map_{name}"
        self._metadata = f"metadata_{name}"
        self._args_kwargs_db = f"__args_kwargs_{name}"
        self._duration_key = "total_duration"
        self._connection = connection
        self._serializer = serializer
        self._pop_job_script = connection.register_script(_POP_JOB_SCRIPT)

    @property
    def total_duration(self) -> float:
        """the total duration in seconds for all queued tasks"""
        try:
            value = self._connection.hget(self._metadata, self._duration_key)
            return float(value)
        except TypeError:
            return 0

    def add(self, job: Job, *args, **kwargs):
        """Adds the item to the queue

        Args:
            job: the item to add to the queue
            args: the positional arguments to pass when enqueueing the job
            kwargs: the key-word arguments to pass when enqueueing the job
        """
        job_json = job.model_dump_json(exclude={"args_kwargs"})
        args_kwargs_data = _serialize_args_kwargs(
            self._serializer, args=args, kwargs=kwargs
        )

        pipe = self._connection.pipeline()
        pipe.hset(self._database, job.storage_id, job_json)
        pipe.hset(self._args_kwargs_db, mapping={job.storage_id: args_kwargs_data})

        pipe.rpush(self.name, job.storage_id)
        pipe.hincrbyfloat(
            self._metadata, self._duration_key, job.estimated_duration or 0
        )

        pipe.execute()

    def empty(self):
        """Empties the queue. This is an alias for clear()"""
        return self.clear()

    def clear(self):
        """Removes all items on the queue"""
        self._connection.delete(
            self._database, self._args_kwargs_db, self.name, self._metadata
        )

    def pop_job(
        self, storage_id: StorageID
    ) -> Tuple[Job, Tuple[Any, ...], Dict[str, Any]]:
        """Removes a job out of this queue and returns it with its args and kwargs

        Args:
            storage_id: the storage id of the job

        Returns:
            the tuple of the job, args and kwargs

        Raises:
            ValidationError: job is of invalid format or does not exist
        """
        # one script, so two pops of the same job cannot both subtract its duration
        job_json, args_kwargs_data = self._pop_job_script(
            keys=[self.name, self._database, self._args_kwargs_db, self._metadata],
            args=[storage_id, self._duration_key, -storage_id.duration],
        )
        job = Job.model_validate_json(job_json)
        args, kwargs = _deserialize_args_kwargs(self._serializer, args_kwargs_data)

        return job, args, kwargs

    def pop_first(
        self, filter_func: Callable[[StorageID, ...], bool], *args, **kwargs
    ) -> Optional[Tuple[Job, Tuple[Any, ...], Dict[str, Any]]]:
        """Pops the first job that fulfills the given filter function

        Args:
            filter_func: the function that receives the job storage id and returns true if condition is fulfilled, else false
            args: extra arguments to pass to the filter function apart from the storage_id
            kwargs: extra key-word arguments to pass to the filter function

        Returns:
            the tuple of the job, args, and kwargs if a job is found fulfilling the condition or None
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
    ) -> Generator[Tuple[Job, Tuple[Any, ...], Dict[str, Any]], None, None]:
        """Pops jobs in FIFO order given a few limitations

        Args:
            max_total_duration: the total seconds the collection of jobs should not exceed; default None

        Returns:
            the generator of tuples of (job, args, kwargs)
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

                job, args, kwargs = self.pop_job(storage_id)
                yield job, args, kwargs

                # update the available duration
                available_duration -= duration

            # move to next batch
            batch += 1


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


def _deserialize_args_kwargs(
    serializer: Serializer, data: bytes
) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
    """Retrieves the args and kwargs for the serialized args kwargs

    Args:
        serializer: the serializer used to serialize them in the past
        data: the serialized args, kwargs data

    Returns:
        a tuple (args, kwargs)
    """
    try:
        data = zlib.decompress(data)
    except zlib.error:
        # Fallback to uncompressed string
        data = data

    return serializer.loads(data)


def _serialize_args_kwargs(
    serializer: Serializer, args: Tuple[Any, ...], kwargs: Dict[str, Any]
) -> bytes:
    """Converts the args and kwargs into bytes to store

    Args:
        serializer: the serializer to convert the args and kwargs
        args: the positional arguments when running the job
        kwargs: the key-word arguments when running the job

    Returns:
        the metadata for the job
    """
    return zlib.compress(serializer.dumps((args, kwargs)))
