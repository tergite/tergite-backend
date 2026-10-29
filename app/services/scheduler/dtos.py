# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025, 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#

"""Definitions for some data transfer objects for the scheduler service"""
import json
from datetime import datetime
from enum import Enum, unique
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator
from pydantic_core.core_schema import SerializationInfo
from rq import Queue as RqQueue
from rq.job import JobStatus

from app.libs.qiskit.qobj import PulseQobj
from app.utils.rq import (
    get_current_job_on_queue,
    get_next_job_on_queue,
    get_previous_job_on_queue,
)

from ...libs.qiskit_providers.utils.json_encoder import IQXJsonEncoder


class RecalibrationInfo(BaseModel):
    """Basic information about recalibration"""

    current_job_id: Optional[str] = None
    current_job_status: Optional[JobStatus] = None
    current_job_started_at: Optional[datetime] = None

    previous_job_id: Optional[str] = None
    previous_job_status: Optional[JobStatus] = None
    previous_job_started_at: Optional[datetime] = None
    previous_job_ended_at: Optional[datetime] = None
    previous_job_error: Optional[str] = None

    next_job_id: Optional[str] = None
    next_job_status: Optional[JobStatus] = None
    next_job_enqueued_at: Optional[datetime] = None

    interval: Optional[float] = None

    @classmethod
    def from_queue(cls, queue: RqQueue) -> "RecalibrationInfo":
        """Creates a RecalibrationInfo object from the queue instance

        Args:
            queue: the rq Queue instance on which recalibration is running

        Returns:
            the RecalibrationInfo object

        Raises:
            RuntimeError: multiple current jobs found: [...]
        """
        current_job = get_current_job_on_queue(queue)
        previous_job = get_previous_job_on_queue(queue)
        next_job = get_next_job_on_queue(queue)

        kwargs = {}
        if previous_job:
            kwargs["previous_job_id"] = previous_job.id
            kwargs["previous_job_status"] = previous_job.get_status()
            kwargs["previous_job_started_at"] = previous_job.started_at
            kwargs["previous_job_ended_at"] = previous_job.ended_at
            # the interval is by default got from the previous job
            kwargs["interval"] = previous_job.meta.get("interval")
            previous_result = previous_job.latest_result()
            if previous_result:
                kwargs["previous_job_error"] = previous_result.exc_string

        if current_job:
            kwargs["current_job_id"] = current_job.id
            kwargs["current_job_status"] = current_job.get_status()
            kwargs["current_job_started_at"] = current_job.started_at
            # the interval is got from the current job if there is no next job
            kwargs["interval"] = current_job.meta.get("interval")

        if next_job:
            kwargs["next_job_id"] = next_job.id
            kwargs["next_job_enqueued_at"] = next_job.enqueued_at
            kwargs["next_job_status"] = next_job.get_status()
            # the interval is got from the next job
            kwargs["interval"] = next_job.meta.get("interval")

        return RecalibrationInfo(**kwargs)


class JobFileParams(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    qobj: PulseQobj

    @field_serializer("qobj")
    def serialize_qobj(self, qobj: PulseQobj, _info: SerializationInfo):
        """Converts qobj into a dict"""
        qobj_dict = qobj.to_dict()

        if _info.mode_is_json():
            return json.dumps(qobj_dict, cls=IQXJsonEncoder)
        return qobj_dict

    @field_validator("qobj", mode="before")
    @classmethod
    def parse_qobj(cls, v):
        """Parses the qobject from dict/str to Qobj"""
        if isinstance(v, PulseQobj):
            return v
        elif isinstance(v, dict):
            return PulseQobj.from_dict(v)
        elif isinstance(v, str):
            return PulseQobj.from_dict(json.loads(v))

        raise TypeError(f"Invalid type for PulseQobj: {type(v)}")


class JobFile(BaseModel):
    """The expected structure of the job file"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    params: JobFileParams


@unique
class LogLevel(Enum):
    """Log level of job supervisor log messages"""

    INFO = 0
    WARNING = 1
    ERROR = 2
