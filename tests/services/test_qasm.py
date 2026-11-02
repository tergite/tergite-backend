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
"""Tests of the translation of gate-level jobs' OpenQASM 3 into native gates"""
import math

import numpy as np
import pytest
from qiskit import qasm3
from qiskit.quantum_info import Operator

from libs.quantum_executor.base import gates
from libs.quantum_executor.base.circuits import (
    CircuitReadout,
    Measurement,
    QasmCircuit,
)
from libs.quantum_executor.base.qasm import to_native_circuit
from tests.utils.circuits import get_qasm, get_unitary, is_equal_up_to_phase
from utils.exc import UnsupportedCircuitError

_DT = 1e-9
_HALF_PI = math.pi / 2

# (OpenQASM statements, the native gates they translate to)
_TRANSLATIONS = [
    ("rz(0.3) $0;", [gates.Rz(0, 0.3)]),
    ("p(0.3) $1;", [gates.Rz(1, 0.3)]),
    ("z $0;", [gates.Rz(0, math.pi)]),
    ("s $0;", [gates.Rz(0, _HALF_PI)]),
    ("sdg $0;", [gates.Rz(0, -_HALF_PI)]),
    ("t $0;", [gates.Rz(0, math.pi / 4)]),
    ("tdg $0;", [gates.Rz(0, -math.pi / 4)]),
    ("rx(0.3) $0;", [gates.Rxy(0, 0.3, 0.0)]),
    ("rx(-pi/2) $1;", [gates.Rxy(1, -_HALF_PI, 0.0)]),
    ("ry(0.3) $0;", [gates.Rxy(0, 0.3, _HALF_PI)]),
    ("x $0;", [gates.Rxy(0, math.pi, 0.0)]),
    ("y $0;", [gates.Rxy(0, math.pi, _HALF_PI)]),
    ("sx $0;", [gates.Rxy(0, _HALF_PI, 0.0)]),
    ("h $0;", [gates.Rz(0, math.pi), gates.Rxy(0, _HALF_PI, _HALF_PI)]),
    (
        "U(0.1, 0.2, 0.3) $0;",
        [gates.Rz(0, 0.3), gates.Rxy(0, 0.1, _HALF_PI), gates.Rz(0, 0.2)],
    ),
    ("cz $0, $1;", [gates.CZ(control=0, target=1)]),
    ("cz $1, $0;", [gates.CZ(control=1, target=0)]),
    (
        "cx $0, $1;",
        [
            gates.Rz(1, math.pi),
            gates.Rxy(1, _HALF_PI, _HALF_PI),
            gates.CZ(control=0, target=1),
            gates.Rz(1, math.pi),
            gates.Rxy(1, _HALF_PI, _HALF_PI),
        ],
    ),
    ("reset $0;", [gates.Reset(0)]),
    ("barrier $0, $1;", [gates.Barrier(qubits=(0, 1))]),
    ("delay[100dt] $1;", [gates.Delay(qubits=(1,), duration=100 * _DT)]),
    ("delay[1.5us] $0;", [gates.Delay(qubits=(0,), duration=1.5e-6)]),
    (
        "delay[20ns] $0, $1;",
        [
            gates.Delay(qubits=(0,), duration=20e-9),
            gates.Delay(qubits=(1,), duration=20e-9),
        ],
    ),
]

# OpenQASM statements whose native gates must make the same unitary
_UNITARIES = [
    "rz(0.7) $0;",
    "p(-1.2) $1;",
    "z $0;",
    "s $0;",
    "sdg $1;",
    "t $0;",
    "tdg $0;",
    "rx(0.7) $0;",
    "ry(-0.4) $1;",
    "x $0;",
    "y $1;",
    "sx $0;",
    "id $0;",
    "h $0;",
    "U(0.1, 0.2, 0.3) $1;",
    "cz $0, $1;",
    "cx $0, $1;",
    "cx $1, $0;",
    "swap $0, $1;",
    "cp(0.4) $0, $1;",
    "crz(0.4) $1, $0;",
    "rx(-pi/2) $0; rz(-pi/2) $0; cz $0, $1; rz(pi/2) $1; sx $1;",
    "gate sxdg _q { s _q; h _q; s _q; }\nsxdg $1;",
    "gate entangle _a, _b { h _a; cx _a, _b; rz(0.3) _b; }\nentangle $1, $0;",
]

# (OpenQASM statements, classical bits, (qubit, clbit, channel) of each measurement)
_MEASUREMENTS = [
    ("x $0;\nc[0] = measure $0;", 1, [(0, 0, 0)]),
    ("x $1;\nc[0] = measure $1;", 1, [(1, 0, 0)]),
    ("c[1] = measure $0;\nc[0] = measure $1;", 2, [(0, 1, 0), (1, 0, 1)]),
    ("c[0] = measure $0;\nc[0] = measure $0;", 1, [(0, 0, 0), (0, 0, 1)]),
    ("c[0] = measure $2;", 3, [(2, 0, 0)]),
]


@pytest.mark.parametrize("statements, expected", _TRANSLATIONS)
def test_gates_translate_to_native_gates(statements, expected):
    circuit = _get_circuit(statements, num_clbits=0)

    got = to_native_circuit(circuit, dt=_DT)

    assert len(got.gates) == len(expected)
    for got_gate, expected_gate in zip(got.gates, expected):
        assert type(got_gate) is type(expected_gate)
        assert _get_fields(got_gate) == pytest.approx(_get_fields(expected_gate))


@pytest.mark.parametrize("statements", _UNITARIES)
def test_native_gates_make_the_circuit_unitary(statements):
    qasm = get_qasm(statements, num_clbits=0)
    expected = Operator(qasm3.loads(qasm)).data

    got = to_native_circuit(_get_circuit(statements, num_clbits=0), dt=_DT)

    unitary = get_unitary(got.gates, num_qubits=got.num_qubits)
    assert is_equal_up_to_phase(unitary, expected)


@pytest.mark.parametrize("statements, num_clbits, expected", _MEASUREMENTS)
def test_measurements_are_acquired_on_channels_in_order(
    statements, num_clbits, expected
):
    circuit = _get_circuit(statements, num_clbits=num_clbits)

    got = to_native_circuit(circuit, dt=_DT)

    measures = [gate for gate in got.gates if isinstance(gate, gates.Measure)]
    assert [(m.qubit, m.clbit, m.channel) for m in measures] == expected
    assert got.num_clbits == num_clbits


def test_physical_qubits_keep_their_index():
    circuit = _get_circuit("x $2;\nc[0] = measure $2;", num_clbits=1)

    got = to_native_circuit(circuit, dt=_DT)

    assert got.num_qubits == 3
    assert got.gates[0] == gates.Rxy(2, math.pi, 0.0)


def test_readout_names_each_measured_qubit_by_its_device_id():
    circuit = _get_circuit("c[1] = measure $0;\nc[0] = measure $1;", num_clbits=2)

    got = to_native_circuit(circuit, dt=_DT).to_readout(qubit_ids=[12, 13, 14])

    assert got == CircuitReadout(
        name="test",
        num_clbits=2,
        measurements=(
            Measurement(qubit_id=12, clbit=1),
            Measurement(qubit_id=13, clbit=0),
        ),
    )


def test_readout_round_trips_through_its_dict():
    readout = CircuitReadout(
        name="bell",
        num_clbits=2,
        measurements=(Measurement(12, 1), Measurement(13, 0)),
    )

    assert CircuitReadout.from_dict(readout.to_dict()) == readout


def test_gate_without_a_definition_is_refused():
    circuit = _get_circuit("c[0] = measure $0;\nif (c[0]) { x $0; }", num_clbits=1)

    with pytest.raises(UnsupportedCircuitError, match="gate if_else is not supported"):
        to_native_circuit(circuit, dt=_DT)


def test_circuit_whose_clbits_differ_from_its_qasm_is_refused():
    circuit = _get_circuit("c[0] = measure $0;", num_clbits=1)
    circuit = circuit.model_copy(update={"num_clbits": 2})

    with pytest.raises(UnsupportedCircuitError, match="declares 2 classical bits"):
        to_native_circuit(circuit, dt=_DT)


def test_invalid_qasm_is_refused():
    circuit = QasmCircuit(name="broken", qasm="OPENQASM 3.0; x", num_clbits=0)

    with pytest.raises(UnsupportedCircuitError, match="not valid OpenQASM 3"):
        to_native_circuit(circuit, dt=_DT)


def _get_circuit(statements: str, num_clbits: int) -> QasmCircuit:
    """Gets a circuit named test of the statements"""
    return QasmCircuit(
        name="test", qasm=get_qasm(statements, num_clbits), num_clbits=num_clbits
    )


def _get_fields(gate: gates.Gate) -> list:
    """Gets the gate's fields as a flat list of numbers, to compare approximately"""
    return list(
        np.hstack([np.asarray(value, dtype=float) for value in vars(gate).values()])
    )
