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
"""Tests that the example configs, which bcc-provision installs for a device on clusters,
describe one device that a worker starts on as they are"""

from pathlib import Path

import settings
from api.worker import preload_worker_globals
from libs.device_parameters import BackendConfig, Device, DeviceCalibration
from services.scheduler import close_device_resources

_ROOT = Path(__file__).parents[1]
_EXAMPLES = {
    "backend_settings": _ROOT / "backend_config.example.toml",
    "calibration_seed": _ROOT / "calibration.seed.example.toml",
    "quantify_config_file": _ROOT / "quantify-config.example.json",
    "quantify_metadata_file": _ROOT / "quantify-metadata.example.yml",
    "calibration_device_config_file": _ROOT / "calib_device_config.example.toml",
    "calibration_node_config_file": _ROOT / "node_config.example.toml",
    "calibration_spi_config_file": _ROOT / "spi_config.example.toml",
}


def test_example_backend_config_and_seed_are_one_device():
    config = BackendConfig.from_toml(
        _EXAMPLES["backend_settings"], _EXAMPLES["calibration_seed"]
    )

    device = Device.from_config(config)
    calibration = DeviceCalibration.from_config(config)

    assert [f"q{qubit.id}" for qubit in calibration.qubits] == device.qubit_ids
    assert {coupler.full_name for coupler in calibration.couplers} == {
        "_".join(qubits) for qubits in device.coupling_dict.values()
    }


def test_a_worker_starts_on_the_examples_as_they_are(tmp_path):
    """as one of a device on clusters does after bcc-provision, its clusters dummies"""
    device_settings = settings.get_device_settings().model_copy(
        update={
            **_EXAMPLES,
            # read as paths' strings, as the env file gives them
            "backend_settings": str(_EXAMPLES["backend_settings"]),
            "calibration_seed": str(_EXAMPLES["calibration_seed"]),
            "booking_db_url": f"sqlite:///{tmp_path}/bcc.db",
            "storage_root": tmp_path,
        }
    )

    device = preload_worker_globals(device_settings, should_load_executor=True)
    close_device_resources(device)
