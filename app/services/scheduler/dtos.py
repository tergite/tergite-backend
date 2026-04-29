# This code is part of Tergite
#
# (C) Chalmers Next Labs 2026
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
from typing import Optional

from pydantic import BaseModel
from rq.job import Job, JobStatus


class RecalibrationInfo(BaseModel):
    """Basic information about recalibration"""

    id: str
    status: JobStatus
    enqueued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    interval: Optional[float] = None
    error: Optional[str] = None

    @classmethod
    def from_job(cls, job: Job) -> "RecalibrationInfo":
        """Creates a RecalibrationInfo object from a job

        Args:
            job: the rq job from which to extract the RecalibrationInfo

        Returns:
            the RecalibrationInfo object
        """
        result = job.latest_result()
        error = None
        if result:
            error = result.exc_string

        return RecalibrationInfo(
            id=job.id,
            status=job.get_status(),
            enqueued_at=job.enqueued_at,
            started_at=job.started_at,
            ended_at=job.ended_at,
            error=error,
            interval=job.meta.get("interval", None),
        )
