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

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import toml

from api.worker import preload_worker_globals
from libs.device_parameters import DeviceCalibration
from services.scheduler import close_device_resources
from tests.utils.api import (
    create_mss_headers,
)
from tests.utils.datetime import get_timestamp_str
from tests.utils.env import TEST_DEFAULT_RECALIBRATION_INTERVAL
from tests.utils.fixtures import load_fixture
from tests.utils.params import (
    CLIENT_AND_WORKER_TUPLES,
    QUANTIFY_CLIENT_AND_WORKER_TUPLES,
    USERS,
)
from tests.utils.recalibrations import (
    get_recalibrations,
    start_due_recalibration_without_running_it,
)
from tests.utils.users import (
    create_many_users,
)
from tests.utils.workers import set_worker_executor, set_worker_settings

_EXPECTED_RECALIBRATION_SEED_CONTENT = load_fixture(
    "expected_recalibration.seed.toml", fmt="toml"
)


@pytest.fixture(autouse=True)
def recalibrate_at_start(monkeypatch):
    """These tests follow the recalibration a device starts with, which is off by default"""
    monkeypatch.setenv("BCC_SHOULD_RECALIBRATE_AT_START", "true")


@pytest.mark.parametrize("client, worker", QUANTIFY_CLIENT_AND_WORKER_TUPLES)
def test_recalibration_sends_the_calibration_to_mss(client, worker, mock_mss):
    with client as client:
        start_up_event_count = len(mock_mss.sent_events)

        worker.run(is_burst=True, max_jobs=1)

        [recalibrated] = mock_mss.sent_events[start_up_event_count:]
        assert recalibrated["name"] == "recalibrated"
        assert recalibrated["data"]["qubits"]


@pytest.mark.parametrize("client, worker", QUANTIFY_CLIENT_AND_WORKER_TUPLES)
def test_recalibration_reaches_what_the_device_runs_and_serves(
    client, worker, mock_mss
):
    """The light workers discriminate with what /dynamic-properties serves, and the
    executor compiles with its calibration, so neither waits for a restart
    """
    with client as client:
        before = client.get("/dynamic-properties", headers=create_mss_headers()).json()

        worker.run(is_burst=True, max_jobs=1)

        [*_, recalibrated] = [
            event["data"]
            for event in mock_mss.sent_events
            if event["name"] == "recalibrated"
        ]
        after = client.get("/dynamic-properties", headers=create_mss_headers()).json()
        executor = next(iter(worker.device_resources.values())).executor
        calibration_config = DeviceCalibration.model_validate(
            recalibrated
        ).to_calibration_config()
        assert before != recalibrated
        assert after == recalibrated
        assert executor.backend_config.calibration_config == calibration_config


@pytest.mark.parametrize("client, worker", QUANTIFY_CLIENT_AND_WORKER_TUPLES)
def test_a_recalibration_starts_from_the_tuners_results_of_the_last(
    client, worker, quantify_seed_file, mocker
):
    """The tuner keeps nothing between its sessions, and deletes its run's own results"""
    from tests.utils.tuner import mock_run_node

    calibration_options = []

    def run_node(*args, **options):
        # the tuner reads it as it creates the session, which the later nodes reuse
        if options.get("session") is None:
            calibration_options.append(options.get("calibration"))
        return mock_run_node(*args, **options)

    mocker.patch(
        "libs.quantum_executor.quantify.utils.calibration.run_node",
        side_effect=run_node,
    )
    results_file = Path(quantify_seed_file).with_suffix(".tuner.json")
    with client as client:
        worker.run(is_burst=True, max_jobs=1)
        first_options = list(calibration_options)
        is_results_file_kept = results_file.exists()
        client.post("/recalibration/init", headers=create_mss_headers(is_admin=True))
        calibration_options.clear()

        worker.run(is_burst=True, max_jobs=1)

    assert first_options == [None]
    assert is_results_file_kept
    assert calibration_options == [str(results_file)]
    assert json.loads(results_file.read_text())["transmons"]


@pytest.mark.parametrize("client, worker", QUANTIFY_CLIENT_AND_WORKER_TUPLES)
def test_init_recalibration_with_interval(client, worker, quantify_seed_file):
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


@pytest.mark.parametrize("client, worker", QUANTIFY_CLIENT_AND_WORKER_TUPLES)
def test_init_recalibration_without_interval(client, worker, quantify_seed_file):
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


@pytest.mark.parametrize("client, worker", QUANTIFY_CLIENT_AND_WORKER_TUPLES)
def test_init_recalibration_with_start_time(
    client, worker, quantify_seed_file, fake_clock
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


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
def test_get_recalibration_info(client, worker, fake_clock):
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


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
def test_cancel_default_recalibration(client, worker):
    """POST /recalibration/cancel cancels the default recalibration started on startup"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS)[0]["id"]

        response = client.post(
            "/recalibration/cancel", headers=create_mss_headers(user_id, is_admin=True)
        )

        assert response.status_code == 200
        assert response.json() == {"status": "success"}
        assert get_recalibrations() == []


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
def test_init_recalibration_reads_a_naive_start_time_as_utc(
    client, worker, monkeypatch
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


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
def test_failed_recalibration_schedules_the_next(client, worker):
    with client as client:
        failing_executor = Mock(recalibrate=Mock(side_effect=RuntimeError("no device")))
        set_worker_executor(worker, failing_executor)

        worker.run(is_burst=True, max_jobs=1)

        [failed, upcoming] = get_recalibrations()
        assert failed.status == "failed"
        assert "no device" in failed.error
        assert upcoming.status == "scheduled"


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
@pytest.mark.parametrize(
    "recalibrations_ttl, expected_kept", [(0, [1, 2]), (3600, [0, 1, 2])]
)
def test_ended_recalibrations_expire_after_their_ttl_but_the_last(
    client, worker, recalibrations_ttl, expected_kept
):
    """The last that ended is /recalibration/info's previous, however long ago it ended"""
    with client as client:
        user_id = create_many_users(client, raw_users=USERS[:1])[0]["id"]
        set_worker_executor(worker, Mock())
        for _ in range(2):
            response = client.post(
                "/recalibration/init",
                headers=create_mss_headers(user_id, is_admin=True),
            )
            assert response.status_code == 200
            worker.run(is_burst=True, max_jobs=1)
        recalibrations = get_recalibrations()

        set_worker_settings(worker, recalibrations_ttl=recalibrations_ttl)
        worker.run(is_burst=True)

        kept_ids = [row.id for row in get_recalibrations()]
        assert [row.status for row in recalibrations] == [
            "finished",
            "finished",
            "scheduled",
        ]
        assert kept_ids == [recalibrations[index].id for index in expected_kept]


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
def test_recalibration_left_started_by_a_dead_worker_fails_as_a_new_one_starts(
    client, worker
):
    with client as client:
        user_id = create_many_users(client, raw_users=USERS[:1])[0]["id"]
        device = next(iter(worker.device_resources.values()))
        start_due_recalibration_without_running_it(device.settings.default_prefix)

        restarted = preload_worker_globals(device.settings, should_load_executor=True)
        close_device_resources(restarted)

        info = client.get(
            "/recalibration/info", headers=create_mss_headers(user_id, is_admin=True)
        ).json()
        [abandoned] = [row for row in get_recalibrations() if row.started_at]
        assert abandoned.status == "failed"
        assert "stopped" in abandoned.error
        assert info["current_job_id"] is None
        assert info["previous_job_id"] == abandoned.id


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
def test_cancel_recalibration_while_one_runs(client, worker):
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


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
def test_cancel_restarted_recalibration(client, worker):
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
