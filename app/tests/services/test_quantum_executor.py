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
import shutil
import socket
from pathlib import Path

import pytest

from ...libs.device_parameters.dtos import BackendConfig
from ..utils.fixtures import get_fixture_path
from ..utils.params import HAS_QUANTIFY

_REAL_HARDWARE_QUANTIFY_CONFIG_FILE = get_fixture_path("generic-quantify-config.json")
_REAL_HARDWARE_QUANTIFY_METADATA_FILE = get_fixture_path("real-quantify-config.yml")
_BACKEND_CONFIG_PATH = get_fixture_path("backend_config.toml")
_CALIBRATION_SEED_FILE = get_fixture_path("quantify.seed.toml")


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
def test_attempts_to_connect_to_real_hardware(tmp_path):
    """Loads the config for the real hardware in the appropriate way"""
    with pytest.raises(socket.timeout):
        backend_conf = BackendConfig.from_toml(
            _BACKEND_CONFIG_PATH, seed_file=_CALIBRATION_SEED_FILE
        )

        from ...libs.quantum_executor.quantify.executor import QuantifyExecutor

        QuantifyExecutor(
            quantify_config_file=_REAL_HARDWARE_QUANTIFY_CONFIG_FILE,
            quantify_metadata_file=_REAL_HARDWARE_QUANTIFY_METADATA_FILE,
            backend_config=backend_conf,
            **_get_calibration_kwargs(tmp_path),
        )


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
def test_recalibrate_refuses_more_than_one_cluster(tmp_path, mocker):
    """tergite_tuner calibrates the whole device through one cluster"""
    from ...libs.quantum_executor.quantify.executor import QuantifyExecutor
    from ...libs.quantum_executor.quantify.utils.config import QuantifyMetadata
    from ..utils.env import TEST_QUANTIFY_CONFIG_FILE, TEST_QUANTIFY_METADATA_FILE

    calibrate = mocker.patch(
        "app.libs.quantum_executor.quantify.executor.recalibrate", return_value=None
    )
    executor = QuantifyExecutor(
        quantify_config_file=TEST_QUANTIFY_CONFIG_FILE,
        quantify_metadata_file=TEST_QUANTIFY_METADATA_FILE,
        backend_config=BackendConfig.from_toml(
            _BACKEND_CONFIG_PATH, seed_file=_CALIBRATION_SEED_FILE
        ),
        **_get_calibration_kwargs(tmp_path),
    )
    cluster = executor.quantify_metadata.root["clusterA"]
    executor.quantify_metadata = QuantifyMetadata(
        {"clusterA": cluster, "clusterB": cluster}
    )

    with pytest.raises(ValueError, match="one cluster"):
        executor.recalibrate()
    calibrate.assert_not_called()


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
def test_executors_of_two_quantify_configs_are_built_one_after_another(tmp_path):
    from ...libs.quantum_executor.quantify.executor import QuantifyExecutor
    from ..utils.env import TEST_QUANTIFY_CONFIG_FILE, TEST_QUANTIFY_METADATA_FILE

    other_metadata_file = tmp_path / "quantify-metadata.yml"
    shutil.copy(TEST_QUANTIFY_METADATA_FILE, other_metadata_file)
    backend_config = BackendConfig.from_toml(
        _BACKEND_CONFIG_PATH, seed_file=_CALIBRATION_SEED_FILE
    )
    QuantifyExecutor(
        quantify_config_file=TEST_QUANTIFY_CONFIG_FILE,
        quantify_metadata_file=TEST_QUANTIFY_METADATA_FILE,
        backend_config=backend_config,
        **_get_calibration_kwargs(tmp_path),
    )

    other_executor = QuantifyExecutor(
        quantify_config_file=TEST_QUANTIFY_CONFIG_FILE,
        quantify_metadata_file=other_metadata_file,
        backend_config=backend_config,
        **_get_calibration_kwargs(tmp_path),
    )

    assert other_executor.quantify_metadata.root


def _get_calibration_kwargs(data_dir: Path) -> dict:
    """Gets the data directory, calibration files and Redis URL a QuantifyExecutor needs"""
    from ..utils.env import (
        TEST_CALIBRATION_DEVICE_CONFIG_FILE,
        TEST_CALIBRATION_NODE_CONFIG_FILE,
        TEST_CALIBRATION_SPI_CONFIG_FILE,
        TEST_QUANTIFY_SEED_FILE,
        TEST_RQ_REDIS_URL,
    )

    return dict(
        data_dir=data_dir,
        calib_node_conf=TEST_CALIBRATION_NODE_CONFIG_FILE,
        calib_device_conf=TEST_CALIBRATION_DEVICE_CONFIG_FILE,
        calib_spi_conf=TEST_CALIBRATION_SPI_CONFIG_FILE,
        calib_seed_file=TEST_QUANTIFY_SEED_FILE,
        redis_url=TEST_RQ_REDIS_URL,
    )
