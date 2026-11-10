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
"""Tests for the order the dispatcher runs its devices' work in, and its recalibrations"""

import threading
import time
from types import SimpleNamespace

from sqlmodel import Session, col, select

from services.scheduler import dispatcher
from services.scheduler.dispatcher import Dispatcher, schedule_recalibration
from services.scheduler.dtos import RecalibrationStatus
from services.scheduler.store import RecalibrationRow
from utils.datetime import get_utc_now
from utils.sql_db import get_sql_engine, migrate_sql_db


def test_a_light_worker_takes_its_devices_in_turn(mocker):
    """So that no device's postprocessing waits behind another's"""
    jobs_left = {"alpha": 2, "beta": 2}
    postprocessed = []

    def postprocess_next(device, deadline=None) -> bool:
        name = device.settings.default_prefix
        if jobs_left[name] == 0:
            return False
        jobs_left[name] -= 1
        postprocessed.append(name)
        return True

    mocker.patch.object(dispatcher, "send_kept_mss_events", return_value=True)
    mocker.patch.object(dispatcher, "fail_overdue_jobs")
    mocker.patch.object(dispatcher, "fail_jobs_queued_too_long")
    mocker.patch.object(dispatcher, "delete_expired_mss_nonces")
    mocker.patch.object(
        dispatcher, "run_next_postprocessing", side_effect=postprocess_next
    )
    devices = {name: _get_device(name) for name in jobs_left}

    Dispatcher(devices, should_run_heavy=False, should_run_light=True).run(
        is_burst=True
    )

    assert postprocessed == ["alpha", "beta", "alpha", "beta"]


def test_a_worker_with_nothing_to_do_waits_the_shortest_of_its_devices_settings(
    mocker,
):
    wait_timeouts = []
    mocker.patch.object(dispatcher, "send_kept_mss_events", return_value=True)
    mocker.patch.object(dispatcher, "fail_overdue_jobs")
    mocker.patch.object(dispatcher, "fail_jobs_queued_too_long")
    mocker.patch.object(dispatcher, "delete_expired_mss_nonces")
    mocker.patch.object(dispatcher, "run_next_postprocessing", return_value=False)
    listener = mocker.patch.object(dispatcher, "WakeUpListener").return_value
    devices = {
        "alpha": _get_device("alpha", max_worker_wait_time=2.5),
        "beta": _get_device("beta", max_worker_wait_time=0.5),
    }
    worker = Dispatcher(devices, should_run_heavy=False, should_run_light=True)

    def record_timeout(timeout: float) -> None:
        wait_timeouts.append(timeout)
        worker.stop()

    listener.wait.side_effect = record_timeout

    worker.run()

    assert wait_timeouts == [0.5]


def test_a_pass_sweeps_the_devices_expired_mss_nonces(mocker):
    """So that no request from MSS spends its time deleting them"""
    mocker.patch.object(dispatcher, "send_kept_mss_events", return_value=True)
    mocker.patch.object(dispatcher, "fail_overdue_jobs")
    mocker.patch.object(dispatcher, "fail_jobs_queued_too_long")
    mocker.patch.object(dispatcher, "run_next_postprocessing", return_value=False)
    sweep = mocker.patch.object(dispatcher, "delete_expired_mss_nonces")
    device = _get_device("alpha")

    Dispatcher({"alpha": device}, should_run_heavy=False, should_run_light=True).run(
        is_burst=True
    )

    sweep.assert_called_with(device.bookings_db)


def test_concurrent_schedulings_leave_one_recalibration_scheduled(tmp_path, mocker):
    """Two would each repeat"""
    db_url = f"sqlite:///{tmp_path}/bcc.db"
    migrate_sql_db(db_url)
    device = SimpleNamespace(
        settings=SimpleNamespace(default_prefix="alpha"),
        bookings_db=get_sql_engine(db_url),
    )
    mocker.patch.object(dispatcher, "wake_workers")

    def get_utc_now_slowly():
        # the other scheduling runs meanwhile
        time.sleep(0.5)
        return get_utc_now()

    mocker.patch.object(dispatcher, "get_utc_now", side_effect=get_utc_now_slowly)
    schedulings = [
        threading.Thread(target=schedule_recalibration, args=(device, None, 60))
        for _ in range(2)
    ]
    for scheduling in schedulings:
        scheduling.start()
    for scheduling in schedulings:
        scheduling.join()

    with Session(device.bookings_db) as session:
        scheduled = session.exec(
            select(RecalibrationRow).where(
                col(RecalibrationRow.status) == RecalibrationStatus.SCHEDULED
            )
        ).all()
    assert len(scheduled) == 1


def _get_device(name: str, max_worker_wait_time: float = 5) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            default_prefix=name,
            mss_response_timeout=1,
            mss_max_reconnection_delay=1,
            max_worker_wait_time=max_worker_wait_time,
        ),
        bookings_db=object(),
        mss_client=None,
    )
