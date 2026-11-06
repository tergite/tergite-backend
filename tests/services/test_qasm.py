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

from libs.quantum_executor.base import gates
from libs.quantum_executor.base.circuits import (
    CircuitReadout,
    Measurement,
    QasmCircuit,
)
from libs.quantum_executor.base.qasm import to_native_circuit
from tests.utils.circuits import (
    get_operator,
    get_qasm,
    get_unitary,
    is_equal_up_to_phase,
)
from utils.exc import UnsupportedCircuitError

_DT = 1e-9
_HALF_PI = math.pi / 2


def _u(theta: float, phi: float, lam: float) -> np.ndarray:
    """OpenQASM's U, which defines its single-qubit gates"""
    cos, sin = math.cos(theta / 2), math.sin(theta / 2)
    return np.array(
        [
            [cos, -np.exp(1j * lam) * sin],
            [np.exp(1j * phi) * sin, np.exp(1j * (phi + lam)) * cos],
        ]
    )


def _rz(theta: float) -> np.ndarray:
    return np.diag([np.exp(-0.5j * theta), np.exp(0.5j * theta)])


def _p(lam: float) -> np.ndarray:
    return np.diag([1, np.exp(1j * lam)])


def _rx(theta: float) -> np.ndarray:
    return _u(theta, -_HALF_PI, _HALF_PI)


def _ry(theta: float) -> np.ndarray:
    return _u(theta, 0.0, 0.0)


def _controlled(matrix: np.ndarray) -> np.ndarray:
    """The matrix controlled by one more qubit, the first"""
    size = len(matrix)
    return np.block(
        [[np.eye(size), np.zeros((size, size))], [np.zeros((size, size)), matrix]]
    )


_X = np.array([[0, 1], [1, 0]])
_Y = np.array([[0, -1j], [1j, 0]])
_Z = np.diag([1, -1])
_H = np.array([[1, 1], [1, -1]]) / math.sqrt(2)
_SX = np.array([[1 + 1j, 1 - 1j], [1 - 1j, 1 + 1j]]) / 2
_SWAP = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]])

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

# (OpenQASM statements, the textbook matrices of their gates and the qubits of each)
_UNITARIES = [
    ("rz(0.7) $0;", [(_rz(0.7), (0,))]),
    ("p(-1.2) $1;", [(_p(-1.2), (1,))]),
    ("z $0;", [(_Z, (0,))]),
    ("s $0;", [(_p(_HALF_PI), (0,))]),
    ("sdg $1;", [(_p(-_HALF_PI), (1,))]),
    ("t $0;", [(_p(math.pi / 4), (0,))]),
    ("tdg $0;", [(_p(-math.pi / 4), (0,))]),
    ("rx(0.7) $0;", [(_rx(0.7), (0,))]),
    ("ry(-0.4) $1;", [(_ry(-0.4), (1,))]),
    ("x $0;", [(_X, (0,))]),
    ("y $1;", [(_Y, (1,))]),
    ("sx $0;", [(_SX, (0,))]),
    ("id $0;", [(np.eye(2), (0,))]),
    ("h $0;", [(_H, (0,))]),
    ("U(0.1, 0.2, 0.3) $1;", [(_u(0.1, 0.2, 0.3), (1,))]),
    ("cz $0, $1;", [(_controlled(_Z), (0, 1))]),
    ("cx $0, $1;", [(_controlled(_X), (0, 1))]),
    ("cx $1, $0;", [(_controlled(_X), (1, 0))]),
    ("swap $0, $1;", [(_SWAP, (0, 1))]),
    ("cy $0, $1;", [(_controlled(_Y), (0, 1))]),
    ("cp(0.4) $0, $1;", [(_controlled(_p(0.4)), (0, 1))]),
    ("cphase(0.4) $1, $0;", [(_controlled(_p(0.4)), (1, 0))]),
    ("crx(0.4) $0, $1;", [(_controlled(_rx(0.4)), (0, 1))]),
    ("cry(0.4) $1, $0;", [(_controlled(_ry(0.4)), (1, 0))]),
    ("crz(0.4) $1, $0;", [(_controlled(_rz(0.4)), (1, 0))]),
    ("ch $0, $1;", [(_controlled(_H), (0, 1))]),
    ("ccx $0, $1, $2;", [(_controlled(_controlled(_X)), (0, 1, 2))]),
    ("cswap $2, $0, $1;", [(_controlled(_SWAP), (2, 0, 1))]),
    (
        "cu(0.1, 0.2, 0.3, 0.4) $0, $1;",
        [(_controlled(np.exp(0.4j) * _u(0.1, 0.2, 0.3)), (0, 1))],
    ),
    ("u2(0.2, 0.3) $0;", [(_u(_HALF_PI, 0.2, 0.3), (0,))]),
    (
        "rx(-pi/2) $0; rz(-pi/2) $0; cz $0, $1; rz(pi/2) $1; sx $1;",
        [
            (_rx(-_HALF_PI), (0,)),
            (_rz(-_HALF_PI), (0,)),
            (_controlled(_Z), (0, 1)),
            (_rz(_HALF_PI), (1,)),
            (_SX, (1,)),
        ],
    ),
    (
        "rz(2 * π / 8 - sin(0.5) ** 2) $0;",
        [(_rz(math.pi / 4 - math.sin(0.5) ** 2), (0,))],
    ),
    ("gate sxdg _q { s _q; h _q; s _q; }\nsxdg $1;", [(_SX.conj().T, (1,))]),
    (
        "gate entangle _a, _b { h _a; cx _a, _b; rz(0.3) _b; }\nentangle $1, $0;",
        [(_H, (1,)), (_controlled(_X), (1, 0)), (_rz(0.3), (0,))],
    ),
    (
        "gate twice(theta) _q { rz(theta) _q; rz(theta) _q; gphase(theta); }\n"
        "twice(0.2) $0;",
        [(_rz(0.4), (0,))],
    ),
    ("qubit[2] q;\nh q[1];\ncx q[1], q[0];", [(_H, (1,)), (_controlled(_X), (1, 0))]),
    ("qubit[2] q;\nx q;", [(_X, (0,)), (_X, (1,))]),
]

# (OpenQASM statements, classical bits, (qubit, clbit, channel) of each measurement)
_MEASUREMENTS = [
    ("x $0;\nc[0] = measure $0;", 1, [(0, 0, 0)]),
    ("x $1;\nc[0] = measure $1;", 1, [(1, 0, 0)]),
    ("c[1] = measure $0;\nc[0] = measure $1;", 2, [(0, 1, 0), (1, 0, 1)]),
    ("c[0] = measure $0;\nc[0] = measure $0;", 1, [(0, 0, 0), (0, 0, 1)]),
    ("c[0] = measure $2;", 3, [(2, 0, 0)]),
    ("measure $1 -> c[0];", 1, [(1, 0, 0)]),
    ("qubit[2] q;\nc = measure q;", 2, [(0, 0, 0), (1, 1, 1)]),
]


@pytest.mark.parametrize("statements, expected", _TRANSLATIONS)
def test_gates_translate_to_native_gates(statements, expected):
    circuit = _get_circuit(statements, num_clbits=0)

    got = to_native_circuit(circuit, dt=_DT)

    assert len(got.gates) == len(expected)
    for got_gate, expected_gate in zip(got.gates, expected):
        assert type(got_gate) is type(expected_gate)
        assert _get_fields(got_gate) == pytest.approx(_get_fields(expected_gate))


@pytest.mark.parametrize("statements, matrices", _UNITARIES)
def test_native_gates_make_the_circuit_unitary(statements, matrices):
    got = to_native_circuit(_get_circuit(statements, num_clbits=0), dt=_DT)

    expected = np.eye(2**got.num_qubits)
    for matrix, qubits in matrices:
        expected = get_operator(matrix, qubits, got.num_qubits) @ expected
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


@pytest.mark.parametrize(
    "statements, message",
    [
        ("c[0] = measure $0;\nif (c[0]) { x $0; }", "if statements are not supported"),
        ("ctrl @ x $0, $1;", "gate modifiers, as ctrl @, are not supported"),
        ("ccz $0, $1, $2;", "gate ccz is not supported"),
        ("gate loop _q { loop _q; }\nloop $0;", "gate loop is not supported"),
        ("input float theta;\nrz(theta) $0;", "inputs and outputs are not supported"),
        ("measure $0;", "a measurement that keeps no result is not supported"),
        ('include "qelib1.inc";\nx $0;', "include qelib1.inc is not supported"),
    ],
    ids=[
        "if",
        "modifier",
        "unknown-gate",
        "self-defined",
        "input",
        "no-result",
        "include",
    ],
)
def test_what_has_no_native_gates_is_refused(statements, message):
    circuit = _get_circuit(statements, num_clbits=1)

    with pytest.raises(UnsupportedCircuitError, match=message):
        to_native_circuit(circuit, dt=_DT)


def test_circuit_whose_clbits_differ_from_its_qasm_is_refused():
    circuit = _get_circuit("c[0] = measure $0;", num_clbits=1)
    circuit = circuit.model_copy(update={"num_clbits": 2})

    with pytest.raises(UnsupportedCircuitError, match="declares 2 classical bits"):
        to_native_circuit(circuit, dt=_DT)


def test_invalid_qasm_is_refused_naming_where_its_error_is():
    qasm = "OPENQASM 3.0;\nqubit[2] q;\nh q[0]\ncx q[0], q[1];"
    circuit = QasmCircuit(name="broken", qasm=qasm, num_clbits=0)

    with pytest.raises(UnsupportedCircuitError) as error:
        to_native_circuit(circuit, dt=_DT)

    assert f"{error.value}" == (
        "circuit broken is not valid OpenQASM 3: line 4, column 0: missing ';' at 'cx'"
    )


def test_invalid_openpulse_is_refused_with_its_parser_s_reason():
    qasm = 'OPENQASM 3.0;\ndefcalgrammar "openpulse";\ncal { play( }\n'
    circuit = QasmCircuit(name="broken", qasm=qasm, num_clbits=0)

    with pytest.raises(UnsupportedCircuitError) as error:
        to_native_circuit(circuit, dt=_DT)

    assert f"{error.value}" == (
        "circuit broken is not valid OpenQASM 3: "
        "Unexpected token '<EOF>' at line 1, column 7."
    )


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
