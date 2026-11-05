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
from types import SimpleNamespace

import pytest

from libs.device_parameters.dtos import BackendConfig

from ..utils.fixtures import get_fixture_path

_REAL_HARDWARE_QUANTIFY_CONFIG_FILE = get_fixture_path("generic-quantify-config.json")
_REAL_HARDWARE_QUANTIFY_METADATA_FILE = get_fixture_path("real-quantify-config.yml")
_BACKEND_CONFIG_PATH = get_fixture_path("backend_config.toml")
_CALIBRATION_SEED_FILE = get_fixture_path("quantify.seed.toml")


def test_attempts_to_connect_to_real_hardware(tmp_path):
    """Loads the config for the real hardware in the appropriate way"""
    with pytest.raises(socket.timeout):
        backend_conf = BackendConfig.from_toml(
            _BACKEND_CONFIG_PATH, seed_file=_CALIBRATION_SEED_FILE
        )

        from libs.quantum_executor.quantify.executor import QuantifyExecutor

        QuantifyExecutor(
            quantify_config_file=_REAL_HARDWARE_QUANTIFY_CONFIG_FILE,
            quantify_metadata_file=_REAL_HARDWARE_QUANTIFY_METADATA_FILE,
            backend_config=backend_conf,
            **_get_calibration_kwargs(tmp_path),
        )


def test_recalibrate_refuses_more_than_one_cluster(tmp_path, mocker):
    """tergite_tuner calibrates the whole device through one cluster"""
    from libs.quantum_executor.quantify.executor import QuantifyExecutor
    from libs.quantum_executor.quantify.utils.config import QuantifyMetadata

    from ..utils.env import TEST_QUANTIFY_CONFIG_FILE, TEST_QUANTIFY_METADATA_FILE

    calibrate = mocker.patch(
        "libs.quantum_executor.quantify.executor.recalibrate", return_value=None
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


def test_executors_of_two_quantify_configs_are_built_one_after_another(tmp_path):
    from libs.quantum_executor.quantify.executor import QuantifyExecutor

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


@pytest.mark.parametrize(
    "tuner_error",
    [None, RuntimeError("tuner failed"), TimeoutError("took longer than 4 seconds")],
    ids=["succeeded", "failed", "timed-out"],
)
def test_executor_reaches_its_clusters_after_a_recalibration(
    tmp_path, mocker, tuner_error
):
    """tergite_tuner closes every cluster as it connects its own, so the executor's next
    job reconnects them, whether the recalibration succeeded, failed or ran out of time
    """
    import qblox_instruments

    from libs.quantum_executor.quantify.executor import QuantifyExecutor

    from ..utils.env import TEST_QUANTIFY_CONFIG_FILE, TEST_QUANTIFY_METADATA_FILE

    def recalibrate(**kwargs):
        qblox_instruments.Cluster.close_all()
        if tuner_error is not None:
            raise tuner_error

    mocker.patch(
        "libs.quantum_executor.quantify.executor.recalibrate", side_effect=recalibrate
    )
    executor = QuantifyExecutor(
        quantify_config_file=TEST_QUANTIFY_CONFIG_FILE,
        quantify_metadata_file=TEST_QUANTIFY_METADATA_FILE,
        backend_config=BackendConfig.from_toml(
            _BACKEND_CONFIG_PATH, seed_file=_CALIBRATION_SEED_FILE
        ),
        **_get_calibration_kwargs(tmp_path),
    )

    try:
        executor.recalibrate()
    except (RuntimeError, TimeoutError):
        pass
    executor._connect_if_stale()

    executor._coordinator.stop()
    assert executor._coordinator.components()


def test_recalibration_may_take_the_transmons_names_and_close_every_instrument(
    tmp_path, mocker
):
    """tergite_tuner creates transmons by the device's qubits' names, and its
    Cluster.close_all is QCoDeS's Instrument.close_all, so the executor gives the names
    up first and its next job gets the quantum device anew"""
    from qcodes import Instrument
    from quantify_scheduler.device_under_test.transmon_element import (
        BasicTransmonElement,
    )

    from libs.quantum_executor.quantify.executor import QuantifyExecutor

    from ..utils.env import TEST_QUANTIFY_CONFIG_FILE, TEST_QUANTIFY_METADATA_FILE

    executor = QuantifyExecutor(
        quantify_config_file=TEST_QUANTIFY_CONFIG_FILE,
        quantify_metadata_file=TEST_QUANTIFY_METADATA_FILE,
        backend_config=BackendConfig.from_toml(
            _BACKEND_CONFIG_PATH, seed_file=_CALIBRATION_SEED_FILE
        ),
        **_get_calibration_kwargs(tmp_path),
    )
    transmon_names = set(executor._quantum_device.elements())

    def recalibrate(**kwargs):
        for name in transmon_names:
            Instrument(name)
        Instrument.close_all()

    mocker.patch(
        "libs.quantum_executor.quantify.executor.recalibrate", side_effect=recalibrate
    )

    executor.recalibrate()
    executor._connect_if_stale()

    device = executor._quantum_device
    assert Instrument.is_valid(device)
    assert set(device.elements()) == transmon_names
    for name in transmon_names:
        element = device.get_element(name)
        assert isinstance(element, BasicTransmonElement)
        assert Instrument.is_valid(element)


def test_executor_refuses_a_missing_tuner_config(tmp_path):
    from libs.quantum_executor.quantify.executor import QuantifyExecutor

    from ..utils.env import (
        TEST_QUANTIFY_CONFIG_FILE,
        TEST_QUANTIFY_METADATA_FILE,
        TEST_QUANTIFY_SEED_FILE,
    )

    backend_config = BackendConfig.from_toml(
        _BACKEND_CONFIG_PATH, seed_file=TEST_QUANTIFY_SEED_FILE
    )
    kwargs = {
        **_get_calibration_kwargs(tmp_path),
        "calib_spi_conf": tmp_path / "x.toml",
    }

    with pytest.raises(FileNotFoundError, match="tergite-tuner's config .*x.toml"):
        QuantifyExecutor(
            TEST_QUANTIFY_CONFIG_FILE,
            TEST_QUANTIFY_METADATA_FILE,
            backend_config,
            **kwargs,
        )


def _get_calibration_kwargs(data_dir: Path) -> dict:
    """Gets the data directory and calibration files a QuantifyExecutor needs"""
    from ..utils.env import (
        TEST_CALIBRATION_DEVICE_CONFIG_FILE,
        TEST_CALIBRATION_NODE_CONFIG_FILE,
        TEST_CALIBRATION_SPI_CONFIG_FILE,
        TEST_QUANTIFY_SEED_FILE,
    )

    return dict(
        data_dir=data_dir,
        calib_node_conf=TEST_CALIBRATION_NODE_CONFIG_FILE,
        calib_device_conf=TEST_CALIBRATION_DEVICE_CONFIG_FILE,
        calib_spi_conf=TEST_CALIBRATION_SPI_CONFIG_FILE,
        calib_seed_file=TEST_QUANTIFY_SEED_FILE,
    )


@pytest.mark.parametrize("is_pulse_limited", [True, False], ids=["limits", "no-limits"])
def test_executor_adds_frames_and_defcals_only_where_the_device_has_pulse_limits(
    is_pulse_limited,
):
    from libs.device_parameters import DeviceCalibration
    from libs.quantum_executor.quantify.executor import QuantifyExecutor

    from ..utils.env import TEST_QUANTIFY_SEED_FILE

    backend_config = BackendConfig.from_toml(
        _BACKEND_CONFIG_PATH, seed_file=TEST_QUANTIFY_SEED_FILE
    )
    if not is_pulse_limited:
        backend_config = backend_config.model_copy(update={"pulse_limits": None})
    # it reads no more of the executor than its backend and hardware configs
    executor = SimpleNamespace(backend_config=backend_config, quantify_config=None)
    calibration = DeviceCalibration.from_config(backend_config)

    got = QuantifyExecutor.add_pulse_calibration(executor, calibration)

    assert bool(got.frames) is is_pulse_limited
    assert bool(got.defcals) is is_pulse_limited
