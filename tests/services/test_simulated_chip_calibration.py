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
"""The 2-qubit simulator, calibrated by tergite-tuner from rough guesses as the
backend recalibrates a chip, and its gate-level jobs on what the tuner found"""
import shutil
from typing import Dict

import numpy as np
import pytest

from libs.device_parameters import BackendConfig
from libs.quantum_executor.base.circuits import QasmCircuit
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from tests.utils.chips import get_chip_config
from tests.utils.circuits import get_qasm
from tests.utils.env import (
    TEST_CALIBRATION_SPI_CONFIG_FILE,
    TEST_QUANTIFY_CONFIG_FILE,
    TEST_QUANTIFY_METADATA_FILE,
)
from tests.utils.fixtures import get_fixture_path

_SHOTS = 1000
_ELEMENTS = ("q12", "q13")
# the whole recalibration takes some three minutes
_TIMEOUT = 900


# what gate-level jobs read on the calibration the tuner finds, q12's bit first
_EXPECTED_SHARES = {
    "x $0;": {"10": 1.0},
    "x $1;": {"01": 1.0},
    "sx $0;\nsx $0;\nx $1;": {"11": 1.0},
    "h $0;\nh $1;\ncz $0, $1;\nh $1;": {"00": 0.5, "11": 0.5},
}


@pytest.mark.timeout(_TIMEOUT)
def test_the_tuner_calibrates_the_chip_from_rough_guesses_for_gate_level_jobs(
    tmp_path, mocker
):
    """The tuner's device config guesses each frequency 0.5 to 1.3 MHz off"""
    from libs.quantum_executor.quantify.executor import QuantifyExecutor

    seed_file = tmp_path / "qiskit_pulse_2q.rough.seed.toml"
    shutil.copy(get_fixture_path("qiskit_pulse_2q.rough.seed.toml"), seed_file)
    executor = QuantifyExecutor(
        quantify_config_file=TEST_QUANTIFY_CONFIG_FILE,
        quantify_metadata_file=TEST_QUANTIFY_METADATA_FILE,
        backend_config=BackendConfig.from_toml(
            get_fixture_path("backend_config.simq2.toml"), seed_file=seed_file
        ),
        data_dir=tmp_path / "data",
        calib_node_conf=get_fixture_path("chip_node_config.toml"),
        calib_device_conf=get_fixture_path("chip_calib_device_config.toml"),
        calib_spi_conf=TEST_CALIBRATION_SPI_CONFIG_FILE,
        calib_seed_file=seed_file,
    )

    calibration = executor.recalibrate()

    chip = get_chip_config()
    found = {f"q{int(qubit.id)}": qubit.frequency.value for qubit in calibration.qubits}
    for name in _ELEMENTS:
        # the tuner's Ramsey runs on both at once, each half excited, so each sees half
        # the other's static ZZ of some -100 kHz
        assert found[name] == pytest.approx(chip.qubits[name].frequency, abs=60e3)
    for body, expected in _EXPECTED_SHARES.items():
        shares = _get_shares(executor, body, mocker)
        for outcome, share in shares.items():
            assert share == pytest.approx(expected.get(outcome, 0.0), abs=0.1), body
    executor.close()


def _get_shares(executor, body: str, mocker) -> Dict[str, float]:
    """Runs the statements, measuring q12 and q13, and gets each outcome's share of the
    shots, q12's bit first, each discriminated as the calibration's LDA does"""
    native_config = NativeQobjConfig.from_measurement_settings(
        meas_level=1, meas_return="single", shots=_SHOTS, n_qubits=2
    )
    qasm = get_qasm(body + "\nc[0] = measure $0;\nc[1] = measure $1;", num_clbits=2)
    circuit = QasmCircuit(name="test", qasm=qasm, num_clbits=2)
    (experiment,) = executor.to_circuit_experiments([circuit], native_config)
    dataset = executor.run_native(
        experiment, native_config=native_config, logger=mocker.MagicMock()
    )
    discriminators = executor.backend_config.calibration_config.discriminators["lda"]
    bits = []
    for channel, name in enumerate(_ELEMENTS):
        values = np.asarray(dataset[channel]).ravel()
        lda = discriminators[name]
        scores = lda["coef_0"] * values.real + lda["coef_1"] * values.imag
        bits.append((scores + lda["intercept"] > 0).astype(int))
    outcomes = [f"{first}{second}" for first, second in zip(*bits)]
    return {
        outcome: outcomes.count(outcome) / _SHOTS
        for outcome in ("00", "01", "10", "11")
    }
