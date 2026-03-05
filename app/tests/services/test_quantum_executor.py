# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2024
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests specific to the quantum executor service"""
import socket

import pytest

from ...libs.device_parameters.dtos import BackendConfig
from ...libs.quantum_executor.quantify.executor import QuantifyExecutor
from ..utils.fixtures import get_fixture_path

_REAL_HARDWARE_QUANTIFY_CONFIG_FILE = get_fixture_path("generic-quantify-config.json")
_REAL_HARDWARE_QUANTIFY_METADATA_FILE = get_fixture_path("real-quantify-config.yml")
_BACKEND_CONFIG_PATH = get_fixture_path("backend_config.toml")
_CALIBRATION_SEED_FILE = get_fixture_path("quantify.seed.toml")


def test_attempts_to_connect_to_real_hardware():
    """Loads the config for the real hardware in the appropriate way"""
    with pytest.raises(socket.timeout):
        backend_conf = BackendConfig.from_toml(
            _BACKEND_CONFIG_PATH, seed_file=_CALIBRATION_SEED_FILE
        )
        QuantifyExecutor(
            quantify_config_file=_REAL_HARDWARE_QUANTIFY_CONFIG_FILE,
            quantify_metadata_file=_REAL_HARDWARE_QUANTIFY_METADATA_FILE,
            backend_config=backend_conf,
        )
