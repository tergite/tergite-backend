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
"""Tests for the queue types"""
from datetime import timedelta

import pytest
import time_machine
from pydantic import ValidationError
from rq import Queue, SimpleWorker, get_current_job
from rq.job import Job as RqJob

from app.libs.queues.dtos import Job
from app.libs.queues.types import RunnerQueue, StaticQueue
from app.utils.rq import get_next_job_on_queue


def test_pop_job_not_in_queue_keeps_total_duration(redis_client):
    queue = StaticQueue("test_queue", connection=redis_client)
    queue.add(Job(device="loke", calibration_date="", estimated_duration=5))
    absent_job = Job(device="loke", calibration_date="", estimated_duration=3)

    with pytest.raises(ValidationError):
        queue.pop_job(absent_job.storage_id)

    assert queue.total_duration == 5


def test_pop_job_subtracts_its_duration(redis_client):
    queue = StaticQueue("test_queue", connection=redis_client)
    job = Job(device="loke", calibration_date="", estimated_duration=3)
    queue.add(Job(device="loke", calibration_date="", estimated_duration=5))
    queue.add(job)

    popped_job, _, _ = queue.pop_job(job.storage_id)

    assert popped_job.job_id == job.job_id
    assert queue.total_duration == 5


def test_clear_deletes_every_key_of_the_queue(redis_client):
    queue = StaticQueue("test_queue", connection=redis_client)
    queue.add(Job(device="loke", calibration_date="", estimated_duration=5), "arg")

    queue.clear()

    assert redis_client.keys("*") == []


def test_total_duration_counts_the_time_left_of_the_running_job(redis_client):
    queue = RunnerQueue(
        "test_queue",
        connection=redis_client,
        job_executor_func=_get_test_queue_total_duration,
    )
    queue.enqueue(
        Job(device="loke", calibration_date="", estimated_duration=10), job_id="first"
    )
    queue.enqueue(
        Job(device="loke", calibration_date="", estimated_duration=5), job_id="second"
    )

    SimpleWorker([queue], connection=redis_client).work(burst=True, max_jobs=1)

    first_job = RqJob.fetch("first", connection=redis_client)
    assert first_job.return_value() == pytest.approx(15, abs=1)


def test_total_duration_stops_counting_a_job_past_its_estimate(redis_client):
    with time_machine.travel(0, tick=False) as clock:
        queue = RunnerQueue("test_queue", connection=redis_client)
        queue.enqueue(
            Job(device="loke", calibration_date="", estimated_duration=10),
            job_id="first",
        )
        SimpleWorker([queue], connection=redis_client).work(burst=True)

        clock.shift(4)
        assert queue.total_duration == 6
        clock.shift(20)
        assert queue.total_duration == 0


def test_next_job_on_queue_is_a_queued_one_before_a_scheduled_one(redis_client):
    queue = Queue("test_queue", connection=redis_client)
    queue.enqueue_in(
        timedelta(minutes=5), "app.libs.queues.utils.noop", job_id="scheduled"
    )
    queue.enqueue("app.libs.queues.utils.noop", job_id="queued")

    assert get_next_job_on_queue(queue).id == "queued"


def _get_test_queue_total_duration(job: Job) -> float:
    connection = get_current_job().connection
    return RunnerQueue("test_queue", connection=connection).total_duration
