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
"""Gate-level circuits and jobs, and the unitaries of native gates"""
import uuid
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from libs.quantum_executor.base import gates

_IDENTITY = np.eye(2, dtype=complex)
_PAULI_X = np.array([[0, 1], [1, 0]], dtype=complex)
_PAULI_Y = np.array([[0, -1j], [1j, 0]], dtype=complex)
_PAULI_Z = np.array([[1, 0], [0, -1]], dtype=complex)


def get_qasm(body: str, num_clbits: int) -> str:
    """Gets an OpenQASM 3 program of the body, as qiskit 2's qasm3.dumps writes one

    Args:
        body: the program's statements, on physical qubits like $0
        num_clbits: the size of its classical register c

    Returns:
        the program
    """
    clbits = f"bit[{num_clbits}] c;\n" if num_clbits else ""
    return f'OPENQASM 3.0;\ninclude "stdgates.inc";\n{clbits}{body}\n'


def get_circuit(name: str, body: str, num_clbits: int) -> Dict[str, Any]:
    """Gets a circuit of a gate-level job's payload

    Args:
        name: the circuit's name
        body: the OpenQASM 3 statements of the circuit
        num_clbits: the number of its classical bits

    Returns:
        the circuit, as uploaded
    """
    return {
        "name": name,
        "qasm": get_qasm(body, num_clbits),
        "num_clbits": num_clbits,
        "metadata": {},
    }


def get_circuit_job(
    circuits: List[Dict[str, Any]],
    shots: int = 100,
    meas_level: int = 2,
    meas_return: str = "single",
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Gets a gate-level job file, as the SDK uploads it

    Args:
        circuits: the circuits of the job
        shots: the number of shots
        meas_level: 1 for IQ points, 2 for discriminated shots
        meas_return: "single" or "avg"
        job_id: the job's id; a new one if None

    Returns:
        the job file's content
    """
    return {
        "job_id": job_id or f"{uuid.uuid4()}",
        "params": {
            "circuits": circuits,
            "shots": shots,
            "meas_level": meas_level,
            "meas_return": meas_return,
        },
    }


def get_unitary(native_gates: Sequence[gates.Gate], num_qubits: int) -> np.ndarray:
    """Gets the unitary of the native gates, qubit 0 the least significant as in qiskit

    Args:
        native_gates: the gates, each a Rxy, Rz or CZ
        num_qubits: the number of qubits they act on

    Returns:
        the unitary of the gates applied in order
    """
    unitary = np.eye(2**num_qubits, dtype=complex)
    for gate in native_gates:
        unitary = _get_gate_unitary(gate, num_qubits) @ unitary
    return unitary


def is_equal_up_to_phase(first: np.ndarray, second: np.ndarray) -> bool:
    """Whether two unitaries differ by a global phase at most

    Args:
        first: one unitary
        second: the other

    Returns:
        True if one is the other times a phase
    """
    overlap = np.trace(first.conj().T @ second)
    if abs(overlap) < 1e-9:
        return False
    phase = overlap / abs(overlap)
    return np.allclose(first * phase, second, atol=1e-9)


def _get_gate_unitary(gate: gates.Gate, num_qubits: int) -> np.ndarray:
    """Gets the unitary of one native gate on all the qubits"""
    if isinstance(gate, gates.Rxy):
        axis = np.cos(gate.phi) * _PAULI_X + np.sin(gate.phi) * _PAULI_Y
        single = np.cos(gate.theta / 2) * _IDENTITY - 1j * np.sin(gate.theta / 2) * axis
        return _get_unitary_on_qubit(single, gate.qubit, num_qubits)
    if isinstance(gate, gates.Rz):
        single = np.diag([np.exp(-0.5j * gate.theta), np.exp(0.5j * gate.theta)])
        return _get_unitary_on_qubit(single, gate.qubit, num_qubits)
    if isinstance(gate, gates.CZ):
        diagonal = [
            -1 if (index >> gate.control) & 1 and (index >> gate.target) & 1 else 1
            for index in range(2**num_qubits)
        ]
        return np.diag(diagonal).astype(complex)
    raise TypeError(f"{gate} has no unitary")


def _get_unitary_on_qubit(
    single: np.ndarray, qubit: int, num_qubits: int
) -> np.ndarray:
    """Gets the unitary of a single-qubit unitary on the qubit, the others left alone"""
    factors = [_IDENTITY] * num_qubits
    factors[num_qubits - 1 - qubit] = single
    unitary = factors[0]
    for factor in factors[1:]:
        unitary = np.kron(unitary, factor)
    return unitary
