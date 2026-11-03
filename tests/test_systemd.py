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
"""Tests for telling systemd that the backend is alive"""
import os
import threading
import time

import pytest

from services.scheduler import WorkerDeadline
from utils.systemd import (
    get_watchdog_seconds,
    keep_watchdog_fed_while_on_time,
    notify_systemd,
)


def test_systemd_is_told_on_its_notify_socket(notify_socket):
    notify_systemd("WATCHDOG=1")

    assert notify_socket.recv(64) == b"WATCHDOG=1"


def test_a_notification_systemd_has_no_room_for_is_dropped(notify_socket):
    """A notification sent while systemd has left the ones before unread, more than
    Linux's 10 or macOS's buffer hold, is dropped rather than waited on or raised"""
    errors = []

    def notify_unread():
        try:
            for _ in range(1000):
                notify_systemd("WATCHDOG=1")
        except Exception as exp:
            errors.append(exp)

    sender = threading.Thread(target=notify_unread, daemon=True)
    sender.start()
    sender.join(timeout=10)

    assert not sender.is_alive() and errors == []
    assert notify_socket.recv(64) == b"WATCHDOG=1"


def test_nothing_is_told_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    notify_systemd("WATCHDOG=1")


@pytest.mark.parametrize(
    "watchdog_usec, watchdog_pid, expected",
    [
        ("60000000", None, 60),
        ("60000000", "own", 60),
        ("60000000", "1", None),
        (None, None, None),
    ],
)
def test_the_watchdog_is_this_processs_when_systemd_set_it_for_it(
    monkeypatch, watchdog_usec, watchdog_pid, expected
):
    for name, value in (
        ("WATCHDOG_USEC", watchdog_usec),
        ("WATCHDOG_PID", watchdog_pid),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(os.getpid()) if value == "own" else value)

    assert get_watchdog_seconds() == expected


def test_the_watchdog_is_fed_only_while_the_worker_is_on_time(notify_socket):
    """So that systemd restarts a worker stuck past its deadline"""
    deadline = WorkerDeadline()
    deadline.set_in(0.5)
    feeder = threading.Thread(
        target=keep_watchdog_fed_while_on_time,
        args=(lambda: deadline.get_seconds_left() > 0, 0.1),
        daemon=True,
    )
    feeder.start()

    time.sleep(1)
    fed = []
    while True:
        try:
            fed.append(notify_socket.recv(64))
        except TimeoutError:
            break
    time.sleep(0.5)
    with pytest.raises(TimeoutError):
        notify_socket.recv(64)

    assert 3 <= len(fed) <= 6
    assert set(fed) == {b"WATCHDOG=1"}
