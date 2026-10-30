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
"""Tests for the wake-ups of waiting workers"""
import os
import subprocess
import sys
import time
from types import SimpleNamespace

from services.scheduler.wakeups import WakeUpListener, wake_workers


def test_wake_up_ends_the_wait_of_its_kind(tmp_path):
    device = _get_device(tmp_path)
    listener = WakeUpListener([device], kinds=["heavy"])

    wake_workers(device, "heavy")
    started_at = time.monotonic()
    listener.wait(timeout=10)
    listener.close()

    assert time.monotonic() - started_at < 5


def test_wake_up_of_another_kind_leaves_the_wait(tmp_path):
    device = _get_device(tmp_path)
    listener = WakeUpListener([device], kinds=["heavy"])

    wake_workers(device, "light")
    started_at = time.monotonic()
    listener.wait(timeout=0.5)
    listener.close()

    assert time.monotonic() - started_at >= 0.5


def test_closed_listener_leaves_no_port_file(tmp_path):
    device = _get_device(tmp_path)
    listener = WakeUpListener([device], kinds=["heavy", "light"])
    port_file_count = len(list(tmp_path.iterdir()))

    listener.close()

    assert (port_file_count, list(tmp_path.iterdir())) == (2, [])


def test_port_file_of_a_dead_worker_is_deleted(tmp_path):
    dead_pid = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (tmp_path / f"heavy-{dead_pid}.port").write_text("1")
    device = _get_device(tmp_path)

    listener = WakeUpListener([device], kinds=["light"])
    port_files = [path.name for path in tmp_path.iterdir()]
    listener.close()

    assert port_files == [f"light-{os.getpid()}.port"]


def _get_device(wakeups_dir) -> SimpleNamespace:
    """Gets a stand-in for a device's resources, of which a listener reads only the folder"""
    return SimpleNamespace(settings=SimpleNamespace(wakeups_dir=wakeups_dir))
