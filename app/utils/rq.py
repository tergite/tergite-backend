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
"""Utilities specific to rq"""
from typing import List, Optional

from redis import Redis
from rq import Queue as RqQueue
from rq import Worker as RqWorker
from rq import exceptions as rq_errors
from rq.job import Job as RqJob
from rq.registry import BaseRegistry


def cancel_rq_job(connection: Redis, job_id: str, ignore_errors: bool = True):
    """Cancels the job if it exists

    Args:
        connection: the connection to the redis server where the queues are
        job_id: the id of the rq job
        ignore_errors: whether to ignore errors; default = True

    Raises:
        rq.exceptions.NoSuchJobError: if the job does not exist
        rq.exceptions.InvalidJobOperationError: if the job has already been cancelled
        rq.exceptions.InvalidJobOperation: if the job has already been cancelled
    """
    try:
        job = RqJob.fetch(job_id, connection=connection)
        job.cancel()
    except (
        rq_errors.NoSuchJobError,
        rq_errors.InvalidJobOperationError,
        rq_errors.InvalidJobOperation,
    ) as exp:
        if not ignore_errors:
            raise exp


def get_current_job_on_queue(queue: RqQueue) -> Optional[RqJob]:
    """Gets the current job for the given queue

    Args:
        queue: the rq queue on which the job is running

    Returns:
        the current job for the queue or None if there is none

    Raises:
        RuntimeError: multiple current jobs found: [...]
    """
    current_jobs = _get_current_jobs_on_queue(queue)
    if len(current_jobs) > 1:
        raise RuntimeError(
            f"multiple current jobs found: {[v.id for v in current_jobs]}"
        )

    try:
        return current_jobs[0]
    except IndexError:
        return None


def get_previous_job_on_queue(queue: RqQueue) -> Optional[RqJob]:
    """Gets the latest previous job for the given queue

    Args:
        queue: the rq queue on which the jobs are running

    Returns:
        the previous job for the queue or None if there is none
    """
    previous_finished_job = _get_last_job_from_registry(queue.finished_job_registry)
    previous_failed_job = _get_last_job_from_registry(queue.failed_job_registry)

    previous_jobs = [
        v for v in (previous_finished_job, previous_failed_job) if v is not None
    ]
    previous_jobs.sort(key=lambda x: x.started_at)

    try:
        # get the latest previous job
        return previous_jobs[-1]
    except IndexError:
        return None


def get_next_job_on_queue(queue: RqQueue) -> Optional[RqJob]:
    """Gets the next job for the given queue

    Args:
        queue: the rq queue on which the jobs are running

    Returns:
        the next job for the queue or None if there is none
    """
    last_scheduled_job = _get_last_job_from_registry(queue.scheduled_job_registry)
    last_queued_job = _get_latest_queued_job(queue)

    next_jobs = [v for v in (last_scheduled_job, last_queued_job) if v is not None]
    next_jobs.sort(key=lambda x: x.started_at)

    try:
        # get the latest previous job
        return next_jobs[-1]
    except IndexError:
        return None


def _get_latest_queued_job(queue: RqQueue) -> Optional[RqJob]:
    """Gets the latest job that has been queued

    Args:
        queue: the rq queue on which the jobs are running

    Returns:
        the job if any or None
    """
    jobs = queue.get_jobs(offset=-1, length=-1)
    try:
        return jobs[0]
    except IndexError:
        return None


def _get_last_job_from_registry(registry: BaseRegistry) -> Optional[RqJob]:
    """Gets the latest job for the given registry

    Args:
        registry: the registry containing the job ids

    Returns:
        the job if any or None
    """
    job_ids = registry.get_job_ids(start=-1, end=-1)
    try:
        return RqJob.fetch(job_ids[0], connection=registry.connection)
    except IndexError:
        return None


def _get_current_jobs_on_queue(queue: RqQueue) -> List[RqJob]:
    """Gets the current jobs for the given queue

    Args:
        queue: the redis queue to get the job for

    Returns:
        the current jobs for the queue
    """
    all_workers = RqWorker.all(connection=queue.connection)
    jobs = [
        worker.get_current_job()
        for worker in all_workers
        if queue.name in worker.queue_names()
    ]
    return [v for v in jobs if v is not None]
