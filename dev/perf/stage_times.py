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
"""Writes each job of PERF_SDK_USER_ID's time in each stage, queued and running, to the CSV file
given, once they have all finished; dev/perf/record.sh runs it after the load"""
import csv
import os
import sys
import time
from datetime import datetime
from typing import List, Optional

import settings
from app.libs.queues.dtos import Job, TimestampPair
from app.services.scheduler import JobsStore
from app.utils.sql_db import get_sql_engine

DRAIN_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 2


def main(path: str) -> None:
    jobs = wait_for_jobs(os.environ["PERF_SDK_USER_ID"])
    with open(path, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["job_id", "status", "stage", "queued_ms", "running_ms"])
        for job in jobs:
            writer.writerows(get_stage_rows(job))


def wait_for_jobs(user_id: str) -> List[Job]:
    """Gets the user's jobs once none is pending or executing, or once the wait times out"""
    deadline = time.time() + DRAIN_TIMEOUT_SECONDS
    device_settings = settings.get_device_settings()
    while True:
        jobs = JobsStore.from_settings(
            get_sql_engine(device_settings.booking_db_url), device_settings
        ).find(user_id=user_id)
        is_drained = all(job.status.is_terminal() for job in jobs)
        if is_drained or time.time() > deadline:
            return jobs
        time.sleep(POLL_INTERVAL_SECONDS)


def get_stage_rows(job: Job) -> List[list]:
    """Gets the job's row for each stage: how long it was queued, then how long it ran"""
    timestamps = job.timestamps
    if timestamps is None:
        return []
    created_at = datetime.fromisoformat(job.created_at)
    preprocessing = timestamps.pre_processing
    execution = timestamps.execution
    postprocessing = timestamps.post_processing
    status = job.status.value
    return [
        [
            job.job_id,
            status,
            "preprocessing",
            _get_ms_between(created_at, _get_start(preprocessing)),
            _get_ms_between(_get_start(preprocessing), _get_finish(preprocessing)),
        ],
        [
            job.job_id,
            status,
            "execution",
            _get_ms_between(_get_finish(preprocessing), _get_start(execution)),
            _get_ms_between(_get_start(execution), _get_finish(execution)),
        ],
        [
            job.job_id,
            status,
            "postprocessing",
            _get_ms_between(_get_finish(execution), _get_start(postprocessing)),
            _get_ms_between(_get_start(postprocessing), _get_finish(postprocessing)),
        ],
    ]


def _get_start(pair: Optional[TimestampPair]) -> Optional[datetime]:
    return None if pair is None else pair.start_timestamp


def _get_finish(pair: Optional[TimestampPair]) -> Optional[datetime]:
    return None if pair is None else pair.finish_timestamp


def _get_ms_between(start: Optional[datetime], end: Optional[datetime]) -> str:
    if start is None or end is None:
        return ""
    return f"{(end - start).total_seconds() * 1000:.1f}"


if __name__ == "__main__":
    main(sys.argv[1])
