# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Tests for the jobs store, which the API and every worker write at once"""

import pytest

from libs.queues.dtos import Job, JobStatus, Stage
from services.scheduler import JobsStore
from utils.sql_db import get_sql_engine, migrate_sql_db

_DEVICE_NAME = "alpha"
_TTL_SECONDS = 3600.0


@pytest.fixture
def jobs_store(tmp_path) -> JobsStore:
    """An empty store of one device, in a database of its own"""
    db_url = f"sqlite:///{tmp_path}/jobs.db"
    migrate_sql_db(db_url)
    return JobsStore(get_sql_engine(db_url), _DEVICE_NAME, ttl=_TTL_SECONDS)


def test_a_job_that_ended_keeps_its_status_through_a_stage_update(jobs_store):
    """A worker moves a job's stage from what it read before; a cancel may come in between"""
    job = jobs_store.insert(Job(device=_DEVICE_NAME, calibration_date=""))
    jobs_store.update(job.job_id, {"status": JobStatus.CANCELLED})

    jobs_store.update(
        job.job_id, {"status": JobStatus.EXECUTING, "stage": Stage.EXEC_W}
    )

    stored_job = jobs_store.get_one(job.job_id)
    assert (stored_job.status, stored_job.stage) == (JobStatus.CANCELLED, Stage.EXEC_W)
