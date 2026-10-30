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

from app.libs.qiskit.qobj import PulseQobj

from ...libs.qiskit_providers.utils.json_encoder import IQXJsonEncoder


@unique
class RecalibrationStatus(str, Enum):
    """Where a recalibration is, in the words RQ had for them, which the API still returns"""

    SCHEDULED = "scheduled"
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"


class RecalibrationInfo(BaseModel):
    """Basic information about recalibration"""

    current_job_id: Optional[str] = None
    current_job_status: Optional[RecalibrationStatus] = None
    current_job_started_at: Optional[datetime] = None

    previous_job_id: Optional[str] = None
    previous_job_status: Optional[RecalibrationStatus] = None
    previous_job_started_at: Optional[datetime] = None
    previous_job_ended_at: Optional[datetime] = None
    previous_job_error: Optional[str] = None

    next_job_id: Optional[str] = None
    next_job_status: Optional[RecalibrationStatus] = None
    next_job_enqueued_at: Optional[datetime] = None

    interval: Optional[float] = None


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
