# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Tests for phase direction handling in Quantify instructions."""

import math
from types import SimpleNamespace

import pytest
from qiskit.qobj import PulseQobj

from app.libs.quantum_executor.quantify.experiment import QuantifyExperiment
from app.libs.quantum_executor.quantify.instruction import (
    _load_calibration_drive_frequencies,
    _load_quantify_modulation_frequencies,
)
from app.libs.quantum_executor.utils.config import load_quantify_config
from app.libs.quantum_executor.utils.portclock import generate_hardware_map
from app.tests.utils.env import TEST_QUANTIFY_CONFIG_FILE, TEST_QUANTIFY_SEED_FILE


def _build_qobj(instructions: list[dict]) -> PulseQobj:
    return PulseQobj.from_dict(
        {
            "qobj_id": "phase-shift-sign-test",
            "header": {
                "backend_name": "quantify",
                "backend_version": "1.0.0",
            },
            "config": {
                "meas_level": 2,
                "meas_return": "single",
                "pulse_library": [],
                "qubit_lo_freq": [0.0],
                "meas_lo_freq": [0.0],
                "memory_slot_size": 100,
                "shots": 1,
                "memory_slots": 1,
                "memory": False,
                "parametric_pulses": [],
                "init_qubits": True,
                "n_qubits": 1,
            },
            "schema_version": "1.2.0",
            "type": "PULSE",
            "experiments": [
                {
                    "header": {"name": "phase_direction"},
                    "instructions": instructions,
                }
            ],
        }
    )


def _build_native_config(qobj: PulseQobj) -> SimpleNamespace:
    return SimpleNamespace(
        acq_return_type=complex,
        protocol=SimpleNamespace(value="SSBIntegrationComplex"),
        bin_mode="append",
        meas_level=qobj.config.meas_level,
        meas_return=qobj.config.meas_return,
        meas_return_cols=qobj.config.memory_slot_size,
        n_qubits=qobj.config.n_qubits,
        shots=qobj.config.shots,
    )


@pytest.fixture(autouse=True)
def clear_phase_reference_caches():
    _load_quantify_modulation_frequencies.cache_clear()
    _load_calibration_drive_frequencies.cache_clear()
    yield
    _load_quantify_modulation_frequencies.cache_clear()
    _load_calibration_drive_frequencies.cache_clear()


def _build_experiment(*, qubit_id: str, instructions: list[dict]) -> QuantifyExperiment:
    quantify_config = load_quantify_config(TEST_QUANTIFY_CONFIG_FILE)
    hardware_map = generate_hardware_map([qubit_id], {}, quantify_config)
    qobj = _build_qobj(instructions)

    return QuantifyExperiment.from_qobj_expt(
        name=qobj.experiments[0].header.name,
        expt=qobj.experiments[0],
        qobj_config=qobj.config,
        native_config=_build_native_config(qobj),
        hardware_map=hardware_map,
        quantify_config_file=TEST_QUANTIFY_CONFIG_FILE,
        calibration_seed_file=TEST_QUANTIFY_SEED_FILE,
    )


def _phase_shift_from_operation(operation) -> float:
    return operation.data["pulse_info"][0]["phase_shift"]


def test_fc_phase_shift_uses_calibration_frequency_instead_of_qobj_setf():
    experiment = _build_experiment(
        qubit_id="q14",
        instructions=[
            {"name": "fc", "t0": 0, "ch": "d0", "phase": math.pi / 2},
            {"name": "setf", "t0": 0, "ch": "d0", "frequency": 4.4},
        ],
    )

    channel = experiment.channel_registry["q14.01"]
    phase_instruction = channel.instructions[0]

    assert phase_instruction.phase == pytest.approx(-90.0)
    assert channel.get_phase_at_position(0) == pytest.approx(-90.0)
    assert _phase_shift_from_operation(
        phase_instruction.to_operation(experiment.config)
    ) == pytest.approx(-90.0)


def test_fc_phase_shift_keeps_direction_when_lo_is_above_calibration_frequency():
    experiment = _build_experiment(
        qubit_id="q12",
        instructions=[
            {"name": "fc", "t0": 0, "ch": "d0", "phase": math.pi / 2},
            {"name": "setf", "t0": 0, "ch": "d0", "frequency": 4.8},
        ],
    )

    channel = experiment.channel_registry["q12.01"]
    phase_instruction = channel.instructions[0]

    assert phase_instruction.phase == pytest.approx(90.0)
    assert channel.get_phase_at_position(0) == pytest.approx(90.0)
    assert _phase_shift_from_operation(
        phase_instruction.to_operation(experiment.config)
    ) == pytest.approx(90.0)


def test_set_phase_instruction_remains_unchanged():
    experiment = _build_experiment(
        qubit_id="q14",
        instructions=[
            {"name": "setp", "t0": 0, "ch": "d0", "phase": math.pi / 2},
        ],
    )

    channel = experiment.channel_registry["q14.01"]
    phase_instruction = channel.instructions[0]

    assert phase_instruction.phase == pytest.approx(90.0)
    assert channel.get_phase_at_position(0) == pytest.approx(90.0)
    assert _phase_shift_from_operation(
        phase_instruction.to_operation(experiment.config)
    ) == pytest.approx(90.0)
