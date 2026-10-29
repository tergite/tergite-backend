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
"""Tests for the device parameters"""
from datetime import datetime, timezone

import time_machine

from app.libs.device_parameters.dtos import _BackendGeneralConfig


def test_online_date_defaults_to_when_the_config_is_made():
    with time_machine.travel(datetime(2030, 1, 1, tzinfo=timezone.utc), tick=False):
        config = _BackendGeneralConfig(
            name="loke", num_qubits=1, num_couplers=0, num_resonators=1, dt=1, dtm=1
        )

    assert config.online_date.startswith("2030-01-01")
