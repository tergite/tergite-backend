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
"""Tests of the pulse schedules of gate-level circuits on the qiskit simulators"""
import math
from typing import List

import pytest

from libs.device_parameters import BackendConfig
from libs.quantum_executor.base import gates
from tests.utils.env import (
    TEST_QISKIT_2Q_SEED_FILE,
    TEST_SIMQ2_BACKEND_SETTINGS_FILE,
)
from tests.utils.params import HAS_QISKIT_DYNAMICS
from utils.exc import UnsupportedCircuitError

pytestmark = pytest.mark.skipif(not HAS_QISKIT_DYNAMICS, reason="requires qiskit")

# the 2-qubit simulator's calibration: pi pulses of 56 ns, a CZ of 88 + 25 + 370 + 88 ns
_DT = 0.01e-9
_PI_PULSE = round(56e-9 / _DT)
_CZ = round((88e-9 + 25e-9 + 370e-9 + 88e-9) / _DT)


@pytest.fixture
def backend_config() -> BackendConfig:
    """The config of the 2-qubit simulator, with its calibration"""
    return BackendConfig.from_toml(
        TEST_SIMQ2_BACKEND_SETTINGS_FILE, seed_file=TEST_QISKIT_2Q_SEED_FILE
    )


def test_measurements_are_acquired_together_at_the_end_into_their_channels(
    backend_config,
):
    from qiskit import pulse

    circuit = _get_circuit(
        [
            gates.Rxy(1, math.pi, 0.0),
            gates.Measure(qubit=1, clbit=0, channel=0),
            gates.Measure(qubit=0, clbit=1, channel=1),
        ]
    )

    got = _get_instructions(backend_config, circuit, pulse.Acquire)

    assert sorted(
        (inst.mem_slot.index, inst.channel.index, time) for time, inst in got
    ) == [(0, 1, _PI_PULSE), (1, 0, _PI_PULSE)]


@pytest.mark.parametrize(
    "theta, expected_amp, expected_angle",
    [
        (math.pi, 0.029, 0.0),
        (math.pi / 2, 0.0145, 0.0),
        (-math.pi / 2, 0.0145, math.pi),
    ],
)
def test_rotation_scales_the_pi_pulse(
    backend_config, theta, expected_amp, expected_angle
):
    from qiskit import pulse

    circuit = _get_circuit([gates.Rxy(0, theta, 0.0)])

    [(time, play)] = _get_instructions(backend_config, circuit, pulse.Play)

    assert time == 0
    assert play.pulse.duration == _PI_PULSE
    assert play.pulse.amp == pytest.approx(expected_amp)
    assert play.pulse.angle == pytest.approx(expected_angle)


def test_rotation_about_y_turns_the_frame_around_its_pulse(backend_config):
    from qiskit import pulse

    circuit = _get_circuit([gates.Rxy(0, math.pi, math.pi / 2)])

    got = _get_instructions(backend_config, circuit, pulse.ShiftPhase)

    assert [(time, inst.phase) for time, inst in got] == [
        (0, pytest.approx(-math.pi / 2)),
        (_PI_PULSE, pytest.approx(math.pi / 2)),
    ]


def test_cz_waits_for_both_qubits_and_corrects_their_phases(backend_config):
    from qiskit import pulse

    circuit = _get_circuit([gates.Rxy(1, math.pi, 0.0), gates.CZ(control=1, target=0)])

    plays = _get_instructions(backend_config, circuit, pulse.Play)
    shifts = _get_instructions(backend_config, circuit, pulse.ShiftPhase)

    cz_start, cz_play = plays[1]
    assert cz_start == _PI_PULSE
    assert cz_play.channel == pulse.ControlChannel(0)
    assert cz_play.duration == _CZ
    # the even qubit, 0, is the control
    assert [(t, i.channel.index, i.phase) for t, i in shifts] == [
        (_PI_PULSE + _CZ, 0, pytest.approx(0.6)),
        (_PI_PULSE + _CZ, 1, pytest.approx(0.3)),
    ]


def test_barrier_aligns_its_qubits(backend_config):
    from qiskit import pulse

    circuit = _get_circuit(
        [
            gates.Rxy(0, math.pi, 0.0),
            gates.Barrier(qubits=(0, 1)),
            gates.Rxy(1, math.pi, 0.0),
        ]
    )

    got = _get_instructions(backend_config, circuit, pulse.Play)

    assert [time for time, _ in got] == [0, _PI_PULSE]


@pytest.mark.parametrize(
    "native_gates",
    [
        [gates.Measure(qubit=0, clbit=0, channel=0), gates.Rxy(1, math.pi, 0.0)],
        [
            gates.Measure(qubit=0, clbit=0, channel=0),
            gates.Measure(qubit=0, clbit=1, channel=1),
        ],
    ],
)
def test_measurement_before_the_end_is_refused(backend_config, native_gates):
    from libs.quantum_executor.qiskit.circuits import get_circuit_schedule

    with pytest.raises(UnsupportedCircuitError, match="measures each qubit once"):
        get_circuit_schedule(_get_circuit(native_gates), backend_config)


def test_reset_of_an_untouched_qubit_does_nothing(backend_config):
    from libs.quantum_executor.qiskit.circuits import get_circuit_schedule

    circuit = _get_circuit([gates.Reset(0), gates.Measure(0, 0, 0)])

    got = get_circuit_schedule(circuit, backend_config)

    assert got.duration == 1


@pytest.mark.parametrize("first", [gates.Rxy(0, math.pi, 0.0), gates.Rz(0, 0.3)])
def test_reset_after_an_operation_is_refused(backend_config, first):
    from libs.quantum_executor.qiskit.circuits import get_circuit_schedule

    circuit = _get_circuit([first, gates.Reset(0)])

    with pytest.raises(UnsupportedCircuitError, match="resets a qubit only before"):
        get_circuit_schedule(circuit, backend_config)


def test_circuit_wider_than_the_simulator_is_refused(backend_config):
    from libs.quantum_executor.qiskit.circuits import get_circuit_schedule

    circuit = gates.NativeCircuit(name="wide", num_qubits=3, num_clbits=0, gates=())

    with pytest.raises(UnsupportedCircuitError, match="has 3 qubits"):
        get_circuit_schedule(circuit, backend_config)


def _get_circuit(native_gates: List[gates.Gate]) -> gates.NativeCircuit:
    """Gets a circuit of the gates on the simulator's two qubits"""
    return gates.NativeCircuit(
        name="test", num_qubits=2, num_clbits=2, gates=tuple(native_gates)
    )


def _get_instructions(
    backend_config: BackendConfig, circuit: gates.NativeCircuit, instruction_type: type
) -> list:
    """Gets the (time, instruction) of the circuit's schedule of the type, in time order"""
    from libs.quantum_executor.qiskit.circuits import get_circuit_schedule

    schedule = get_circuit_schedule(circuit, backend_config)
    return [
        (time, instruction)
        for time, instruction in schedule.instructions
        if isinstance(instruction, instruction_type)
    ]
