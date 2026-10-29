# This code is part of Tergite
#
# (C) Martin Ahindura 2023, 2024
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
"""Tests for the API's recalibration routes"""
import math
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import toml
from redis import Redis
from rq.job import Job as RqJob

from app.libs.queues.dtos import get_queue_context
from app.tests.utils.api import (
    create_mss_headers,
)
from app.tests.utils.datetime import get_timestamp_str
from app.tests.utils.env import (
    TEST_DEFAULT_RECALIBRATION_INTERVAL,
    TEST_MAX_RECALIBRATION_QUEUE_TIME,
)
from app.tests.utils.fixtures import load_fixture
from app.tests.utils.params import (
    CLIENT_AND_RQ_WORKER_TUPLES,
    HAS_QUANTIFY,
    USERS,
)
from app.tests.utils.rq import get_queue_by_name, wait_on_rq_worker
from app.tests.utils.users import (
    create_many_users,
)

_EXPECTED_RECALIBRATION_SEED_CONTENT = load_fixture(
    "expected_recalibration.seed.toml", fmt="toml"
)


@pytest.mark.skipif(not HAS_QUANTIFY, reason="the simulators send no calibration")
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_recalibration_sends_the_calibration_to_mss(
    client, redis_conn, worker, mock_mss
):
    with client as client:
        start_up_event_count = len(mock_mss.sent_events)

        worker.work(burst=True, max_jobs=1)

        [recalibrated] = mock_mss.sent_events[start_up_event_count:]
        assert recalibrated["name"] == "recalibrated"
        assert recalibrated["data"]["qubits"]


@pytest.mark.skipif(
    not HAS_QUANTIFY,
    reason="the seed this checks is written by quantify's recalibration",
)
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_init_recalibration_with_interval(
    client, redis_conn: Redis, worker, quantify_seed_file, fake_clock
):
    """Init recalibration restarts the calibration sequence, changing the interval from the default"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        initial_interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        max_queue_time = TEST_MAX_RECALIBRATION_QUEUE_TIME
        new_interval = 4

        non_admin_headers = create_mss_headers(user_id, is_admin=False)
        admin_headers = create_mss_headers(user_id, is_admin=True)

        initial_rq_job = queue.jobs[-1]
        initial_rq_interval = initial_rq_job.meta.get("interval", None)

        # non admins are not allowed
        response = client.post("/recalibration/init", headers=non_admin_headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}

        before_request_timestamp = datetime.now(timezone.utc)
        # admins are allowed
        response = client.post(
            "/recalibration/init",
            headers=admin_headers,
            params={"interval": new_interval},
        )
        actual_output = response.json()

        assert response.status_code == 200
        assert actual_output == {"status": "success"}

        wait_on_rq_worker(
            worker,
            max_jobs=2,
            max_idle_time=initial_interval + max_queue_time,
            with_scheduler=True,
            queue=queue,
            clock=fake_clock,
        )

        final_rq_job_id = queue.finished_job_registry.get_job_ids(start=-1, end=-1)[0]
        final_rq_job = RqJob.fetch(final_rq_job_id, connection=redis_conn)
        final_rq_interval = final_rq_job.meta.get("interval", None)
        final_actual_interval = (
            final_rq_job.started_at - before_request_timestamp
        ).total_seconds()
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        assert initial_rq_interval == initial_interval
        assert final_rq_interval == new_interval
        assert final_rq_job.is_finished
        assert initial_rq_job.is_canceled
        assert math.isclose(final_actual_interval, new_interval, abs_tol=1)
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.skipif(
    not HAS_QUANTIFY,
    reason="the seed this checks is written by quantify's recalibration",
)
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_init_recalibration_without_interval(
    client, redis_conn: Redis, worker, quantify_seed_file, fake_clock
):
    """Init recalibration restarts the calibration sequence without interval continues with the default interval"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        initial_interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        max_queue_time = TEST_MAX_RECALIBRATION_QUEUE_TIME
        admin_headers = create_mss_headers(user_id, is_admin=True)
        initial_rq_job = queue.jobs[-1]
        initial_rq_interval = initial_rq_job.meta.get("interval", None)

        before_request_timestamp = datetime.now(timezone.utc)
        # admins are allowed
        response = client.post(
            "/recalibration/init",
            headers=admin_headers,
        )
        actual_output = response.json()

        assert response.status_code == 200
        assert actual_output == {"status": "success"}

        wait_on_rq_worker(
            worker,
            max_jobs=2,
            max_idle_time=initial_interval + max_queue_time,
            with_scheduler=True,
            queue=queue,
            clock=fake_clock,
        )

        final_rq_job_id = queue.finished_job_registry.get_job_ids(start=-1, end=-1)[0]
        final_rq_job = RqJob.fetch(final_rq_job_id, connection=redis_conn)
        final_rq_interval = final_rq_job.meta.get("interval", None)
        final_actual_interval = (
            final_rq_job.started_at - before_request_timestamp
        ).total_seconds()
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        assert initial_rq_interval == initial_interval
        assert final_rq_interval == initial_interval
        assert final_rq_job.is_finished
        assert initial_rq_job.is_canceled
        assert math.isclose(final_actual_interval, initial_interval, abs_tol=1)
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.skipif(
    not HAS_QUANTIFY,
    reason="the seed this checks is written by quantify's recalibration",
)
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_init_recalibration_with_start_time(
    client, redis_conn: Redis, worker, quantify_seed_file, fake_clock
):
    """POST /recalibration/cancel restarts the recalibration sequence at a given start time in future"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        initial_interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        max_queue_time = TEST_MAX_RECALIBRATION_QUEUE_TIME
        headers = create_mss_headers(user_id, is_admin=True)

        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)
        # admins are allowed
        response = client.post(
            "/recalibration/init",
            headers=headers,
            params={"start_timestamp": start_timestamp.isoformat()},
        )
        actual_output = response.json()

        assert response.status_code == 200
        assert actual_output == {"status": "success"}

        wait_on_rq_worker(
            worker,
            max_jobs=1,
            max_idle_time=initial_interval + max_queue_time,
            with_scheduler=True,
            queue=queue,
            clock=fake_clock,
        )

        first_rq_job_id = queue.finished_job_registry.get_job_ids(start=0, end=1)[0]
        first_rq_job = RqJob.fetch(first_rq_job_id, connection=redis_conn)
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        assert first_rq_job.is_finished
        assert math.isclose(
            first_rq_job.started_at.timestamp(), start_timestamp.timestamp(), abs_tol=1
        )
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_get_recalibration_info(client, redis_conn: Redis, worker, fake_clock):
    """Get /recalibration/info gets recalibration info retrieves the basic recalibration info"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        initial_interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        max_queue_time = TEST_MAX_RECALIBRATION_QUEUE_TIME
        new_interval = 4.0

        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/recalibration/info", headers=headers)
        assert response.status_code == 200
        initial_info = response.json()

        headers = create_mss_headers(user_id, is_admin=True)
        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)
        response = client.post(
            "/recalibration/init",
            headers=headers,
            params={
                "start_timestamp": start_timestamp.isoformat(),
                "interval": new_interval,
            },
        )
        assert response.status_code == 200

        # get info after altering recalibration
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/recalibration/info", headers=headers)
        assert response.status_code == 200
        pre_run_info = response.json()

        wait_on_rq_worker(
            worker,
            max_jobs=2,
            max_idle_time=initial_interval + max_queue_time,
            with_scheduler=True,
            queue=queue,
            clock=fake_clock,
        )

        # get info after running the worker
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.get("/recalibration/info", headers=headers)
        assert response.status_code == 200
        post_run_info = response.json()

        cancelled_rq_job_id = queue.canceled_job_registry.get_job_ids(start=0, end=1)[0]
        cancelled_rq_job = RqJob.fetch(cancelled_rq_job_id, connection=redis_conn)

        first_finished_rq_job_id = queue.finished_job_registry.get_job_ids(
            start=0, end=1
        )[0]

        last_finished_rq_job_id = queue.finished_job_registry.get_job_ids(
            start=-1, end=-1
        )[0]
        last_finished_rq_job = RqJob.fetch(
            last_finished_rq_job_id, connection=redis_conn
        )

        next_rq_job_id = queue.scheduled_job_registry.get_job_ids(start=0, end=1)[0]

        def to_datetime_str(v: datetime) -> str:
            return get_timestamp_str(v, timespec="microseconds")

        # asserts of info shown
        assert initial_info == {
            "current_job_id": None,
            "current_job_status": None,
            "current_job_started_at": None,
            "previous_job_id": None,
            "previous_job_status": None,
            "previous_job_started_at": None,
            "previous_job_ended_at": None,
            "previous_job_error": None,
            "next_job_id": cancelled_rq_job_id,
            "next_job_status": "queued",
            "next_job_enqueued_at": to_datetime_str(cancelled_rq_job.enqueued_at),
            "interval": initial_interval,
        }
        assert pre_run_info == {
            "current_job_id": None,
            "current_job_status": None,
            "current_job_started_at": None,
            "previous_job_id": None,
            "previous_job_status": None,
            "previous_job_started_at": None,
            "previous_job_ended_at": None,
            "previous_job_error": None,
            "next_job_id": first_finished_rq_job_id,
            "next_job_status": "scheduled",
            "next_job_enqueued_at": None,
            "interval": new_interval,
        }
        assert post_run_info == {
            "current_job_id": None,
            "current_job_status": None,
            "current_job_started_at": None,
            "previous_job_id": last_finished_rq_job_id,
            "previous_job_status": "finished",
            "previous_job_started_at": to_datetime_str(last_finished_rq_job.started_at),
            "previous_job_ended_at": to_datetime_str(last_finished_rq_job.ended_at),
            "previous_job_error": None,
            "next_job_id": next_rq_job_id,
            "next_job_status": "scheduled",
            "next_job_enqueued_at": None,
            "interval": new_interval,
        }


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_cancel_default_recalibration(client, redis_conn: Redis, worker):
    """POST /recalibration/cancel cancels the default recalibration started on startup"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)

        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/recalibration/cancel", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        assert queue.jobs == []
        assert queue.canceled_job_registry.count == 1
        assert queue.scheduled_job_registry.count == 0
        assert queue.finished_job_registry.count == 0
        assert queue.failed_job_registry.count == 0
        assert queue.deferred_job_registry.count == 0


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_init_recalibration_reads_a_naive_start_time_as_utc(
    client, redis_conn, worker, monkeypatch
):
    """POST /recalibration/init schedules a start time without a timezone in UTC,
    whatever the server's own timezone"""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        with client as client:
            queue_name = f"{get_queue_context()['queue_prefix']}_recalibration"
            registry = get_queue_by_name(worker, queue_name).scheduled_job_registry
            start_utc = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
                hours=1
            )

            client.post(
                "/recalibration/init",
                headers=create_mss_headers(is_admin=True),
                params={"start_timestamp": start_utc.replace(tzinfo=None).isoformat()},
            )

            [rq_job_id] = registry.get_job_ids()
            assert registry.get_scheduled_time(rq_job_id) == start_utc
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_failed_recalibration_schedules_the_next(client, redis_conn, worker, mocker):
    with client as client:
        queue_name = f"{get_queue_context()['queue_prefix']}_recalibration"
        queue = get_queue_by_name(worker, queue_name)
        failing_executor = Mock(recalibrate=Mock(side_effect=RuntimeError("no device")))
        mocker.patch(
            "app.services.scheduler.tasks.get_quantum_executor",
            return_value=failing_executor,
        )

        worker.work(burst=True, max_jobs=1)

        assert queue.failed_job_registry.count == 1
        assert queue.scheduled_job_registry.count == 1


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_cancel_recalibration_while_one_runs(client, redis_conn, worker, mocker):
    """POST /recalibration/cancel during a recalibration leaves none to follow it"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS[:1])[0]["id"]
        queue_name = f"{get_queue_context()['queue_prefix']}_recalibration"
        queue = get_queue_by_name(worker, queue_name)

        def cancel_recalibration():
            headers = create_mss_headers(user_id, is_admin=True)
            client.post("/recalibration/cancel", headers=headers)

        cancelling_executor = Mock(recalibrate=Mock(side_effect=cancel_recalibration))
        mocker.patch(
            "app.services.scheduler.tasks.get_quantum_executor",
            return_value=cancelling_executor,
        )

        worker.work(burst=True, max_jobs=1)

        assert queue.jobs == []
        assert queue.scheduled_job_registry.count == 0


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_RQ_WORKER_TUPLES)
def test_cancel_restarted_recalibration(client, redis_conn: Redis, worker):
    """POST /recalibration/cancel cancels any recalibration restarted even after cancellation"""
    with client as client:
        users = create_many_users(client, raw_users=USERS)
        user = users[0]
        user_id = user["id"]
        queue_prefix = get_queue_context()["queue_prefix"]
        queue_name = f"{queue_prefix}_recalibration"
        queue = get_queue_by_name(worker, queue_name)

        # first cancel the recalibration
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/recalibration/cancel", headers=headers)
        assert response.status_code == 200

        # restart the recalibration with a given interval
        headers = create_mss_headers(user_id, is_admin=True)
        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)
        response = client.post(
            "/recalibration/init",
            headers=headers,
            params={
                "start_timestamp": start_timestamp.isoformat(),
                "interval": 4.0,
            },
        )
        assert response.status_code == 200

        assert queue.canceled_job_registry.count == 1
        assert queue.scheduled_job_registry.count == 1

        # Cancel the recalibration again
        headers = create_mss_headers(user_id, is_admin=True)
        response = client.post("/recalibration/cancel", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "success"}

        assert queue.jobs == []
        assert queue.canceled_job_registry.count == 2
        assert queue.scheduled_job_registry.count == 0
        assert queue.finished_job_registry.count == 0
        assert queue.failed_job_registry.count == 0
        assert queue.deferred_job_registry.count == 0
