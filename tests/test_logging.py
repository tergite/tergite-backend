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
"""Tests for the logging the application sets up"""

import logging

from utils.logging import SharedRotatingFileHandler


def test_a_log_two_processes_rotate_keeps_every_line_in_order(tmp_path):
    """Each worker process opens the device's job supervisor log on its own"""
    log_file = tmp_path / "shared.log"
    handlers = [
        SharedRotatingFileHandler(log_file, max_bytes=100, backup_count=50)
        for _ in range(2)
    ]
    messages = [f"line {index:02}" for index in range(40)]

    for index, message in enumerate(messages):
        record = logging.makeLogRecord({"msg": message, "levelno": logging.ERROR})
        handlers[index % 2].handle(record)
    for handler in handlers:
        handler.close()

    backups = sorted(
        tmp_path.glob("shared.log.*"),
        key=lambda file: int(file.suffix[1:]),
        reverse=True,
    )
    log_files = [*backups, log_file]
    lines = [line for file in log_files for line in file.read_text().splitlines()]
    assert lines == messages
    assert all(file.stat().st_size <= 100 for file in log_files)
