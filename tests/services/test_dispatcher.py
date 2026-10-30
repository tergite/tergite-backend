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
"""Tests for the order the dispatcher runs its devices' work in"""
from types import SimpleNamespace

from services.scheduler import dispatcher
from services.scheduler.dispatcher import Dispatcher


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
    mocker.patch.object(
        dispatcher, "run_next_postprocessing", side_effect=postprocess_next
    )
    devices = {name: _get_device(name) for name in jobs_left}

    Dispatcher(devices, should_run_heavy=False, should_run_light=True).run(
        is_burst=True
    )

    assert postprocessed == ["alpha", "beta", "alpha", "beta"]


def _get_device(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            default_prefix=name, mss_response_timeout=1, mss_max_reconnection_delay=1
        ),
        bookings_db=None,
        mss_client=None,
    )
