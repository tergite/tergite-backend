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
import pytest
from pydantic import ValidationError

from app.libs.queues.dtos import Job
from app.libs.queues.types import StaticQueue


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
