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
"""Wakes a waiting worker when its device has work for it: an empty datagram to the loopback
port that the worker names in a file of the device's folder, as `<kind>-<pid>.port`

UDP, not a Unix socket in the device's folder, whose path can pass the 104 bytes macOS allows.
"""
import os
import select
import socket
from contextlib import suppress
from pathlib import Path
from typing import Iterable, Literal

from .resources import DeviceResources

WorkerKind = Literal["heavy", "light"]

_LOOPBACK_HOST = "127.0.0.1"


class WakeUpListener:
    """The port a worker waits for wake-ups on, named in its devices' folders until closed"""

    def __init__(self, devices: Iterable[DeviceResources], kinds: Iterable[WorkerKind]):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((_LOOPBACK_HOST, 0))
        self._socket.setblocking(False)
        self._port = self._socket.getsockname()[1]
        self._port_files: list[Path] = []
        for device in devices:
            folder = device.settings.wakeups_dir
            folder.mkdir(parents=True, exist_ok=True)
            _delete_dead_port_files(folder)
            for kind in kinds:
                port_file = folder / f"{kind}-{os.getpid()}.port"
                port_file.write_text(f"{self._port}")
                self._port_files.append(port_file)

    def wait(self, timeout: float) -> None:
        """Waits for a wake-up, or for the timeout, taking every wake-up sent

        Args:
            timeout: the most seconds to wait
        """
        select.select([self._socket], [], [], max(timeout, 0))
        with suppress(BlockingIOError):
            while True:
                self._socket.recv(1)

    def wake(self) -> None:
        """Ends the wait under way, or else the next"""
        _send_wake_up(self._port)

    def close(self) -> None:
        """Stops listening, and deletes the port files that name this listener"""
        for port_file in self._port_files:
            port_file.unlink(missing_ok=True)
        self._socket.close()


def wake_workers(device: DeviceResources, kind: WorkerKind) -> None:
    """Wakes the device's waiting workers of the kind; a busy one looks for work when done

    Args:
        device: what the device runs on
        kind: the kind of workers to wake, "heavy" or "light"
    """
    for port_file in device.settings.wakeups_dir.glob(f"{kind}-*.port"):
        # a file half written, or deleted meanwhile, costs that worker its wait's timeout
        with suppress(OSError, ValueError):
            _send_wake_up(int(port_file.read_text()))


def _send_wake_up(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(b"", (_LOOPBACK_HOST, port))


def _delete_dead_port_files(folder: Path) -> None:
    """Deletes the port files that workers killed before closing their listeners left"""
    for port_file in folder.glob("*-*.port"):
        with suppress(ValueError):
            pid = int(port_file.stem.rsplit("-", 1)[1])
            if not _is_running(pid):
                port_file.unlink(missing_ok=True)


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # another user's
        pass
    return True
