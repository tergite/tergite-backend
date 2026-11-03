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
"""Telling systemd that the process is alive, for a unit with WatchdogSec, which restarts it
when told nothing for that long; a datagram on NOTIFY_SOCKET, as sd_notify sends it"""
import errno
import os
import socket
import time
from typing import Callable, Optional


def notify_systemd(message: str) -> None:
    """Sends systemd the message, e.g. WATCHDOG=1, if it runs this process; else nothing.
    A message systemd has no room for, as it has not read those before, is dropped

    Args:
        message: the notification to send
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    # an abstract socket's name starts with @, which stands for its leading NUL
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
        # Linux queues 10 unread datagrams and blocks the next sender, which would stall
        # bcc start's watch of its children; the next notification follows anyway
        sender.setblocking(False)
        try:
            sender.sendto(message.encode(), address)
        except BlockingIOError:
            pass
        except OSError as exp:
            # macOS refuses the datagram instead
            if exp.errno != errno.ENOBUFS:
                raise


def get_watchdog_seconds() -> Optional[float]:
    """Gets the WatchdogSec systemd set for this process

    Returns:
        the watchdog's seconds, or None if systemd set none for this process
    """
    microseconds = os.environ.get("WATCHDOG_USEC")
    watchdog_pid = os.environ.get("WATCHDOG_PID")
    if microseconds is None or (watchdog_pid and int(watchdog_pid) != os.getpid()):
        return None
    return int(microseconds) / 1_000_000


def keep_watchdog_fed_while_on_time(
    is_on_time: Callable[[], bool], interval_seconds: float
) -> None:
    """Tells systemd the process is alive every interval for as long as it is on time, which
    a thread does beside the work, so that systemd restarts a process that falls behind, or
    whose work holds the interpreter

    Args:
        is_on_time: tells whether the process is still on time
        interval_seconds: the seconds between notifications
    """
    while is_on_time():
        notify_systemd("WATCHDOG=1")
        time.sleep(interval_seconds)
