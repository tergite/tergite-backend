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
"""Tests for the job supervisor log"""

import re

from services.scheduler.utils import log_job_error
from utils.config import DeviceSettings


def test_job_error_is_a_plain_line_stamped_in_utc(tmp_path):
    device_settings = DeviceSettings(
        default_prefix="alpha", jwt_secret="secret", storage_root=tmp_path
    )

    log_job_error(device_settings, "Job j1 failed")
    log_job_error(device_settings, "Job j2 failed")

    lines = device_settings.job_supervisor_log.read_text().splitlines()
    pattern = r"\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\] ERROR: Job j\d failed"
    assert len(lines) == 2 and all(re.fullmatch(pattern, line) for line in lines)


def test_job_supervisor_log_is_rotated_as_its_settings_say(tmp_path):
    device_settings = DeviceSettings(
        default_prefix="alpha",
        jwt_secret="secret",
        storage_root=tmp_path,
        job_supervisor_log_max_bytes=200,
        job_supervisor_log_backup_count=2,
    )

    for index in range(20):
        log_job_error(device_settings, f"Job j{index} failed")

    log_file = device_settings.job_supervisor_log
    log_files = sorted(log_file.parent.glob("job_supervisor.log*"))
    assert [file.name for file in log_files] == [
        "job_supervisor.log",
        "job_supervisor.log.1",
        "job_supervisor.log.2",
    ]
    assert all(file.stat().st_size <= 200 for file in log_files)
    assert log_file.read_text().splitlines()[-1].endswith("Job j19 failed")


def test_job_supervisor_log_is_never_rotated_with_a_max_size_of_0(tmp_path):
    device_settings = DeviceSettings(
        default_prefix="alpha",
        jwt_secret="secret",
        storage_root=tmp_path,
        job_supervisor_log_max_bytes=0,
    )

    for index in range(20):
        log_job_error(device_settings, f"Job j{index} failed")

    log_file = device_settings.job_supervisor_log
    assert list(log_file.parent.glob("job_supervisor.log*")) == [log_file]
    assert len(log_file.read_text().splitlines()) == 20
