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

from app.services.scheduler.utils import log_job_error


def test_job_error_is_a_plain_line_stamped_in_utc(tmp_path):
    log_file = tmp_path / "job_supervisor.log"

    log_job_error(log_file, "Job j1 failed")
    log_job_error(log_file, "Job j2 failed")

    lines = log_file.read_text().splitlines()
    pattern = r"\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\] ERROR: Job j\d failed"
    assert len(lines) == 2 and all(re.fullmatch(pattern, line) for line in lines)
