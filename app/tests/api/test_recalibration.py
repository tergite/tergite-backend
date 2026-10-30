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

from app.tests.utils.api import (
    create_mss_headers,
)
from app.tests.utils.datetime import get_timestamp_str
from app.tests.utils.env import TEST_DEFAULT_RECALIBRATION_INTERVAL
from app.tests.utils.fixtures import load_fixture
from app.tests.utils.params import (
    CLIENT_AND_WORKER_TUPLES,
    HAS_QUANTIFY,
    USERS,
)
from app.tests.utils.recalibrations import get_recalibrations
from app.tests.utils.users import (
    create_many_users,
)
from app.tests.utils.workers import set_worker_executor

_EXPECTED_RECALIBRATION_SEED_CONTENT = load_fixture(
    "expected_recalibration.seed.toml", fmt="toml"
)


@pytest.mark.skipif(not HAS_QUANTIFY, reason="the simulators send no calibration")
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_recalibration_sends_the_calibration_to_mss(
    client, redis_conn, worker, mock_mss
):
    with client as client:
        start_up_event_count = len(mock_mss.sent_events)

        worker.run(is_burst=True, max_jobs=1)

        [recalibrated] = mock_mss.sent_events[start_up_event_count:]
        assert recalibrated["name"] == "recalibrated"
        assert recalibrated["data"]["qubits"]


@pytest.mark.skipif(
    not HAS_QUANTIFY,
    reason="the seed this checks is written by quantify's recalibration",
)
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_init_recalibration_with_interval(
    client, redis_conn, worker, quantify_seed_file
):
    """Init recalibration restarts the calibration sequence, changing the interval from the default"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS)[0]["id"]
        new_interval = 4
        [start_up_recalibration] = get_recalibrations()

        # non admins are not allowed
        response = client.post(
            "/recalibration/init", headers=create_mss_headers(user_id, is_admin=False)
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}

        response = client.post(
            "/recalibration/init",
            headers=create_mss_headers(user_id, is_admin=True),
            params={"interval": new_interval},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "success"}

        worker.run(is_burst=True, max_jobs=1)

        [finished, upcoming] = get_recalibrations()
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        assert start_up_recalibration.interval == TEST_DEFAULT_RECALIBRATION_INTERVAL
        assert (finished.status, finished.interval) == ("finished", new_interval)
        assert (upcoming.status, upcoming.interval) == ("scheduled", new_interval)
        assert upcoming.due_at == finished.started_at + timedelta(seconds=new_interval)
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.skipif(
    not HAS_QUANTIFY,
    reason="the seed this checks is written by quantify's recalibration",
)
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_init_recalibration_without_interval(
    client, redis_conn, worker, quantify_seed_file
):
    """Init recalibration restarts the calibration sequence without interval continues with the default interval"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS)[0]["id"]
        [start_up_recalibration] = get_recalibrations()

        response = client.post(
            "/recalibration/init", headers=create_mss_headers(user_id, is_admin=True)
        )
        assert response.status_code == 200
        assert response.json() == {"status": "success"}

        worker.run(is_burst=True, max_jobs=1)

        [finished, upcoming] = get_recalibrations()
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        interval = TEST_DEFAULT_RECALIBRATION_INTERVAL
        assert finished.id != start_up_recalibration.id
        assert (finished.status, finished.interval) == ("finished", interval)
        assert upcoming.due_at == finished.started_at + timedelta(seconds=interval)
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.skipif(
    not HAS_QUANTIFY,
    reason="the seed this checks is written by quantify's recalibration",
)
@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_init_recalibration_with_start_time(
    client, redis_conn, worker, quantify_seed_file, fake_clock
):
    """POST /recalibration/init restarts the recalibration sequence at a given start time in future"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS)[0]["id"]
        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)

        response = client.post(
            "/recalibration/init",
            headers=create_mss_headers(user_id, is_admin=True),
            params={"start_timestamp": start_timestamp.isoformat()},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "success"}

        assert not worker.run(is_burst=True, max_jobs=1)
        fake_clock.move_to(start_timestamp)
        worker.run(is_burst=True, max_jobs=1)

        [finished, _] = get_recalibrations()
        with open(quantify_seed_file, "r") as file:
            seed_file_content = toml.load(file)

        assert finished.status == "finished"
        assert math.isclose(
            _to_aware(finished.started_at).timestamp(),
            start_timestamp.timestamp(),
            abs_tol=1,
        )
        assert seed_file_content == _EXPECTED_RECALIBRATION_SEED_CONTENT


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_get_recalibration_info(client, redis_conn, worker, fake_clock):
    """Get /recalibration/info gets recalibration info retrieves the basic recalibration info"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS)[0]["id"]
        new_interval = 4.0

        response = client.get(
            "/recalibration/info", headers=create_mss_headers(user_id, is_admin=True)
        )
        assert response.status_code == 200
        initial_info = response.json()
        [start_up_recalibration] = get_recalibrations()

        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)
        response = client.post(
            "/recalibration/init",
            headers=create_mss_headers(user_id, is_admin=True),
            params={
                "start_timestamp": start_timestamp.isoformat(),
                "interval": new_interval,
            },
        )
        assert response.status_code == 200

        response = client.get(
            "/recalibration/info", headers=create_mss_headers(user_id, is_admin=True)
        )
        assert response.status_code == 200
        pre_run_info = response.json()
        [scheduled] = get_recalibrations()

        fake_clock.move_to(start_timestamp)
        worker.run(is_burst=True, max_jobs=1)

        response = client.get(
            "/recalibration/info", headers=create_mss_headers(user_id, is_admin=True)
        )
        assert response.status_code == 200
        post_run_info = response.json()
        [finished, upcoming] = get_recalibrations()

        assert initial_info == {
            "current_job_id": None,
            "current_job_status": None,
            "current_job_started_at": None,
            "previous_job_id": None,
            "previous_job_status": None,
            "previous_job_started_at": None,
            "previous_job_ended_at": None,
            "previous_job_error": None,
            "next_job_id": start_up_recalibration.id,
            "next_job_status": "scheduled",
            "next_job_enqueued_at": _to_info_str(start_up_recalibration.due_at),
            "interval": TEST_DEFAULT_RECALIBRATION_INTERVAL,
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
            "next_job_id": scheduled.id,
            "next_job_status": "scheduled",
            "next_job_enqueued_at": _to_info_str(start_timestamp),
            "interval": new_interval,
        }
        assert post_run_info == {
            "current_job_id": None,
            "current_job_status": None,
            "current_job_started_at": None,
            "previous_job_id": finished.id,
            "previous_job_status": "finished",
            "previous_job_started_at": _to_info_str(finished.started_at),
            "previous_job_ended_at": _to_info_str(finished.ended_at),
            "previous_job_error": None,
            "next_job_id": upcoming.id,
            "next_job_status": "scheduled",
            "next_job_enqueued_at": _to_info_str(upcoming.due_at),
            "interval": new_interval,
        }


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_cancel_default_recalibration(client, redis_conn, worker):
    """POST /recalibration/cancel cancels the default recalibration started on startup"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS)[0]["id"]

        response = client.post(
            "/recalibration/cancel", headers=create_mss_headers(user_id, is_admin=True)
        )

        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        assert get_recalibrations() == []


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_init_recalibration_reads_a_naive_start_time_as_utc(
    client, redis_conn, worker, monkeypatch
):
    """POST /recalibration/init schedules a start time without a timezone in UTC,
    whatever the server's own timezone"""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        with client as client:
            start_utc = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
                hours=1
            )

            client.post(
                "/recalibration/init",
                headers=create_mss_headers(is_admin=True),
                params={"start_timestamp": start_utc.replace(tzinfo=None).isoformat()},
            )

            [scheduled] = get_recalibrations()
            assert _to_aware(scheduled.due_at) == start_utc
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_failed_recalibration_schedules_the_next(client, redis_conn, worker):
    with client as client:
        failing_executor = Mock(recalibrate=Mock(side_effect=RuntimeError("no device")))
        set_worker_executor(worker, failing_executor)

        worker.run(is_burst=True, max_jobs=1)

        [failed, upcoming] = get_recalibrations()
        assert failed.status == "failed"
        assert "no device" in failed.error
        assert upcoming.status == "scheduled"


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_cancel_recalibration_while_one_runs(client, redis_conn, worker):
    """POST /recalibration/cancel during a recalibration leaves none to follow it"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS[:1])[0]["id"]

        def cancel_recalibration():
            headers = create_mss_headers(user_id, is_admin=True)
            client.post("/recalibration/cancel", headers=headers)

        cancelling_executor = Mock(recalibrate=Mock(side_effect=cancel_recalibration))
        set_worker_executor(worker, cancelling_executor)

        worker.run(is_burst=True, max_jobs=1)

        assert [row.status for row in get_recalibrations()] == ["finished"]


@pytest.mark.parametrize("client, redis_conn, worker", CLIENT_AND_WORKER_TUPLES)
def test_cancel_restarted_recalibration(client, redis_conn, worker):
    """POST /recalibration/cancel cancels any recalibration restarted even after cancellation"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS)[0]["id"]

        response = client.post(
            "/recalibration/cancel", headers=create_mss_headers(user_id, is_admin=True)
        )
        assert response.status_code == 200

        start_timestamp = datetime.now(timezone.utc) + timedelta(seconds=2)
        response = client.post(
            "/recalibration/init",
            headers=create_mss_headers(user_id, is_admin=True),
            params={
                "start_timestamp": start_timestamp.isoformat(),
                "interval": 4.0,
            },
        )
        assert response.status_code == 200
        assert [row.status for row in get_recalibrations()] == ["scheduled"]

        response = client.post(
            "/recalibration/cancel", headers=create_mss_headers(user_id, is_admin=True)
        )
        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        assert get_recalibrations() == []


def _to_aware(value: datetime) -> datetime:
    """Gets the UTC datetime that SQLite gave back without its timezone"""
    return value.replace(tzinfo=timezone.utc)


def _to_info_str(value: datetime) -> str:
    return get_timestamp_str(_to_aware(value), timespec="microseconds")
