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
"""Utilities for testing on a simulated chip behind the dummy clusters"""
from pathlib import Path

import toml

from libs.device_parameters import BackendConfig
from libs.device_parameters.dtos import ChipConfig
from tests.utils.circuits import get_gate_level_seed
from tests.utils.env import TEST_BACKEND_SETTINGS_FILE, TEST_QUANTIFY_CONFIG_FILE
from tests.utils.fixtures import get_fixture_path


def get_chip_config() -> ChipConfig:
    """Gets the test chip, of q12, q13 and q14 and the coupler of q12 and q13

    Returns:
        the chip's true parameters
    """
    return ChipConfig.model_validate(toml.load(get_fixture_path("simulated_chip.toml")))


def get_chip_backend_config(directory: Path) -> BackendConfig:
    """Gets the test backend config with the gate-level seed and the test chip

    Args:
        directory: where to write the seed

    Returns:
        the backend config, its simulator section the test chip's
    """
    seed_file = directory / "seed.toml"
    seed_file.write_text(toml.dumps(get_gate_level_seed()))
    config = BackendConfig.from_toml(TEST_BACKEND_SETTINGS_FILE, seed_file=seed_file)
    simulator = {"chip": get_chip_config()}
    return BackendConfig.model_validate({**config.model_dump(), "simulator": simulator})


def get_clusters_device(name: str, directory: Path):
    """Gets a quantum device of the gate-level seed's calibration on the dummy clusters

    Args:
        name: the device's name, unique in the process
        directory: where to write the seed

    Returns:
        the quantum device, which the caller closes
    """
    from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

    from libs.quantum_executor.quantify.device import apply_calibration
    from libs.quantum_executor.quantify.utils.config import load_quantify_config

    device = QuantumDevice(name)
    apply_calibration(device, get_chip_backend_config(directory))
    device.hardware_config(load_quantify_config(TEST_QUANTIFY_CONFIG_FILE))
    return device
