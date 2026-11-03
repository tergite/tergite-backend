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
from typing import Annotated, Any, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Tag,
    field_serializer,
    field_validator,
)
from pydantic_core.core_schema import SerializationInfo

from libs.qiskit.qobj import PulseQobj
from libs.qiskit_providers.utils.json_encoder import IQXJsonEncoder
from libs.quantum_executor.base.circuits import CircuitJobParams


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
    """The params of a job file: the qobject to run"""

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    qobj: PulseQobj

    @field_serializer("qobj")
    def serialize_qobj(self, qobj: PulseQobj, _info: SerializationInfo):
        """Converts qobj into a dict

        Args:
            qobj: the qobject to serialize
            _info: the serialization info, which tells whether it serializes to JSON

        Returns:
            the qobject's dict, or its JSON string when serializing to JSON
        """
        qobj_dict = qobj.to_dict()

        if _info.mode_is_json():
            return json.dumps(qobj_dict, cls=IQXJsonEncoder)
        return qobj_dict

    @field_validator("qobj", mode="before")
    @classmethod
    def parse_qobj(cls, v):
        """Parses the qobject from dict/str to Qobj

        Args:
            v: the qobject, its dict or its JSON string

        Returns:
            the qobject

        Raises:
            TypeError: Invalid type for PulseQobj: {type}
        """
        if isinstance(v, PulseQobj):
            return v
        elif isinstance(v, dict):
            return PulseQobj.from_dict(v)
        elif isinstance(v, str):
            return PulseQobj.from_dict(json.loads(v))

        raise TypeError(f"Invalid type for PulseQobj: {type(v)}")


def get_job_params_kind(value: Any) -> str:
    """Gets which params a job file has, which JobFile validates them as

    Args:
        value: the params, as uploaded or as a model

    Returns:
        "circuits" for a gate-level job's, else "qobj"
    """
    if isinstance(value, dict):
        return "circuits" if "circuits" in value else "qobj"
    return "circuits" if isinstance(value, CircuitJobParams) else "qobj"


class JobFile(BaseModel):
    """The expected structure of the job file: its params are a qobj of pulses, or the
    OpenQASM 3 circuits of a gate-level job"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    params: Annotated[
        Union[
            Annotated[JobFileParams, Tag("qobj")],
            Annotated[CircuitJobParams, Tag("circuits")],
        ],
        Discriminator(get_job_params_kind),
    ]
