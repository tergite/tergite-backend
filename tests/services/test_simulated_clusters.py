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
"""Tests of the dummy clusters whose acquisitions are a simulated chip's"""
from pathlib import Path

import numpy as np
import pytest
from qblox_instruments import ClusterType
from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.enums import BinMode
from quantify_scheduler.operations.control_flow_library import LoopOperation
from quantify_scheduler.operations.gate_library import Measure, Reset, X

from libs.quantum_executor.base.circuits import QasmCircuit
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from tests.utils.chips import get_chip_backend_config, get_clusters_device
from tests.utils.circuits import get_qasm
from tests.utils.env import (
    TEST_CALIBRATION_DEVICE_CONFIG_FILE,
    TEST_CALIBRATION_NODE_CONFIG_FILE,
    TEST_CALIBRATION_SPI_CONFIG_FILE,
    TEST_QUANTIFY_CONFIG_FILE,
    TEST_QUANTIFY_METADATA_FILE,
    TEST_QUANTIFY_SEED_FILE,
)
from tests.utils.fixtures import get_fixture_path

_SHOTS = 400


@pytest.fixture
def executor(tmp_path):
    """A quantify executor of the test device with the test chip"""
    from libs.quantum_executor.quantify.executor import QuantifyExecutor

    executor = QuantifyExecutor(
        quantify_config_file=TEST_QUANTIFY_CONFIG_FILE,
        quantify_metadata_file=TEST_QUANTIFY_METADATA_FILE,
        backend_config=get_chip_backend_config(tmp_path),
        **_get_calibration_kwargs(tmp_path),
    )
    yield executor
    executor.close()


def test_a_device_with_a_chip_plays_its_circuits_on_it(executor, mocker):
    native_config = NativeQobjConfig.from_measurement_settings(
        meas_level=1, meas_return="single", shots=_SHOTS, n_qubits=3
    )
    qasm = get_qasm("x $0;\nc[0] = measure $0;\nc[1] = measure $1;", num_clbits=2)
    circuit = QasmCircuit(name="x", qasm=qasm, num_clbits=2)

    (experiment,) = executor.to_circuit_experiments([circuit], native_config)
    dataset = executor.run_native(
        experiment, native_config=native_config, logger=mocker.MagicMock()
    )

    excited, ground = (np.asarray(dataset[channel]).ravel() for channel in (0, 1))
    assert len(excited) == len(ground) == _SHOTS
    # q12's readout of |1> and |0> lie apart, its shots about the one it was left in
    assert abs(np.median(excited) - np.median(ground)) > 5 * np.std(ground)


@pytest.mark.parametrize(
    "bin_mode, loops, expected_dims",
    [
        (BinMode.AVERAGE, 0, ("acq_index_0",)),
        (BinMode.APPEND, 0, ("repetition", "acq_index_0")),
        (BinMode.APPEND, 4, ("repetition", "loop_repetition")),
    ],
)
def test_the_clusters_return_the_chips_acquisitions_as_quantify_shapes_them(
    executor, tmp_path, bin_mode, loops, expected_dims
):
    device = get_clusters_device("test_simulated_clusters", tmp_path)
    shot = Schedule("shot")
    shot.add(Reset("q12"))
    shot.add(X("q12"))
    shot.add(Measure("q12", acq_index=0, bin_mode=bin_mode))
    shot.add(Reset("q12"))
    shot.add(Measure("q12", acq_index=1, bin_mode=bin_mode))
    schedule = Schedule("test", repetitions=300)
    schedule.add(LoopOperation(body=shot, repetitions=loops) if loops else shot)
    compiled = SerialCompiler("test").compile(
        schedule, config=device.generate_compilation_config()
    )
    device.close()

    coordinator = executor._coordinator
    coordinator.prepare(compiled)
    coordinator.start()
    coordinator.wait_done(timeout_sec=10)
    values = coordinator.retrieve_acquisition()[0]
    coordinator.stop()

    assert values.dims == expected_dims
    shots = np.asarray(values).reshape(-1, 2)
    expected = executor._chip.run(compiled)[0].values.reshape(-1, 2)
    # the chip's |1> and |0> responses, whatever the noise of its shots
    assert np.allclose(shots.mean(axis=0), expected.mean(axis=0), atol=2e-3)
    assert abs(shots[:, 0].mean() - shots[:, 1].mean()) > 5e-3


def test_a_chip_behind_a_real_cluster_is_refused(tmp_path):
    from libs.quantum_executor.quantify.executor import QuantifyExecutor

    with pytest.raises(ValueError, match="dummy clusters"):
        QuantifyExecutor(
            quantify_config_file=get_fixture_path("generic-quantify-config.json"),
            quantify_metadata_file=get_fixture_path("real-quantify-config.yml"),
            backend_config=get_chip_backend_config(tmp_path),
            **_get_calibration_kwargs(tmp_path),
        )


def _get_calibration_kwargs(data_dir: Path) -> dict:
    """Gets the data directory and calibration files a QuantifyExecutor needs"""
    return dict(
        data_dir=data_dir,
        calib_node_conf=TEST_CALIBRATION_NODE_CONFIG_FILE,
        calib_device_conf=TEST_CALIBRATION_DEVICE_CONFIG_FILE,
        calib_spi_conf=TEST_CALIBRATION_SPI_CONFIG_FILE,
        calib_seed_file=TEST_QUANTIFY_SEED_FILE,
    )


def test_the_tuners_hardware_connects_to_the_chip_in_its_real_cluster_mode(tmp_path):
    """Fails if tergite-tuner stops connecting through the Cluster and ClusterComponent
    of its backend/hardware.py, which a simulated chip's recalibration swaps out"""
    from tergite_tuner import SessionContext
    from tergite_tuner.backend import Hardware
    from tergite_tuner.utils.types.enums import MeasurementMode, SPIMode

    from libs.quantum_executor.quantify.simulated_chip.component import (
        SimulatingClusterComponent,
    )
    from libs.quantum_executor.quantify.simulated_chip.engine import SimulatedChip
    from libs.quantum_executor.quantify.simulated_chip.tuner import (
        connect_tuner_to_chip,
    )
    from libs.quantum_executor.quantify.utils.config import load_quantify_config

    session = SessionContext.from_env(
        None,
        cluster_mode=MeasurementMode.real,
        spi_mode=SPIMode.dummy,
        is_recalibration=True,
        cluster_ip="192.0.2.141",
        qubits=["q12"],
        data_dir=tmp_path,
        cluster_config=load_quantify_config(TEST_QUANTIFY_CONFIG_FILE),
        device_config=str(TEST_CALIBRATION_DEVICE_CONFIG_FILE),
        node_config=str(TEST_CALIBRATION_NODE_CONFIG_FILE),
        spi_config=str(TEST_CALIBRATION_SPI_CONFIG_FILE),
    )
    chip = SimulatedChip(get_chip_backend_config(tmp_path).simulator.chip)

    with connect_tuner_to_chip({17: ClusterType.CLUSTER_QRM_RF}, chip):
        hardware = Hardware(session)

    component = hardware.coordinator.get_component(f"ic_{hardware.cluster.name}")
    assert isinstance(component, SimulatingClusterComponent)
    assert component.chip is chip
    assert list(hardware.cluster.get_connected_modules()) == [17]


def test_a_chips_recalibration_runs_the_tuner_on_it_in_its_real_mode(executor, mocker):
    from tergite_tuner.backend import hardware
    from tergite_tuner.utils.types.enums import MeasurementMode

    def recalibrate(**options):
        assert options["cluster_mode"] == MeasurementMode.real
        assert hardware.ClusterComponent.keywords["chip"] is executor._chip
        assert hardware.Cluster.__name__ == "ChipCluster"

    mocker.patch(
        "libs.quantum_executor.quantify.executor.recalibrate", side_effect=recalibrate
    )

    executor.recalibrate()

    assert hardware.Cluster.__name__ == "Cluster"
