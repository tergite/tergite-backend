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
"""The socket systemd would listen on for a process's notifications, in the tests"""
import os
import socket
import tempfile

import pytest


@pytest.fixture
def notify_socket(monkeypatch):
    """The socket systemd listens on, as NOTIFY_SOCKET names it, short enough for macOS"""
    folder = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(folder, "notify")
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(path)
    receiver.settimeout(0.1)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    yield receiver
    receiver.close()
    os.unlink(path)
    os.rmdir(folder)
