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
from datetime import datetime
from enum import Enum, unique
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from libs.quantum_executor.base.circuits import CircuitJobParams

_QOBJ_REFUSAL = (
    "the backend takes OpenQASM 3 circuits only, not a qobj; "
    "upgrade the tergite SDK to send them"
)


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


class JobFile(BaseModel):
    """The expected structure of the job file: the OpenQASM 3 circuits of a gate-level job"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    params: CircuitJobParams

    @field_validator("params", mode="before")
    @classmethod
    def reject_qobj(cls, value: Any) -> Any:
        """Rejects the qobj that an SDK older than gate-level jobs sends

        Args:
            value: the params, as uploaded

        Returns:
            the params, unchanged

        Raises:
            ValueError: the backend takes OpenQASM 3 circuits only, not a qobj; ...
        """
        if isinstance(value, dict) and "qobj" in value:
            raise ValueError(_QOBJ_REFUSAL)
        return value
