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
#
# The decompositions follow those of qpi-driver's `to_quantify_gates`
# (https://github.com/sopherapps/qpi, MIT License, (C) Copyright Martin Ahindura 2026).
"""Translates the OpenQASM 3 circuits of gate-level jobs into native gates"""
import math
from typing import List, Sequence

from qiskit import QuantumCircuit, qasm3
from qiskit.circuit import CircuitInstruction, ControlFlowOp, Delay

from utils.exc import UnsupportedCircuitError

from . import gates
from .circuits import QasmCircuit

_SECONDS_PER_UNIT = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12}


def to_native_circuit(circuit: QasmCircuit, dt: float) -> gates.NativeCircuit:
    """Parses the circuit's OpenQASM 3 and translates its gates into native gates

    Gates beyond the native ones are decomposed into them, up to a global phase: those of
    OpenQASM's standard library that a table names, and any other by its definition. Each
    measurement is acquired on its own channel, numbered in the order the circuit
    measures.

    Args:
        circuit: the circuit as uploaded
        dt: the device's sample time in seconds, which delays in dt are counted in

    Returns:
        the circuit of native gates

    Raises:
        UnsupportedCircuitError: circuit {name} is not valid OpenQASM 3: {error}
        UnsupportedCircuitError: circuit {name} declares {n} classical bits, but its
            OpenQASM has {m}
        UnsupportedCircuitError: gate {name} is not supported
        UnsupportedCircuitError: gate {name} has a parameter that is not a number
        UnsupportedCircuitError: delay unit {unit} is not supported
    """
    quantum_circuit = _load_circuit(circuit)
    if quantum_circuit.num_clbits != circuit.num_clbits:
        raise UnsupportedCircuitError(
            f"circuit {circuit.name} declares {circuit.num_clbits} classical bits, "
            f"but its OpenQASM has {quantum_circuit.num_clbits}"
        )

    native_gates: List[gates.Gate] = []
    for instruction in quantum_circuit.data:
        _add_native_gates(
            native_gates,
            instruction,
            qubits=[quantum_circuit.find_bit(q).index for q in instruction.qubits],
            clbits=[quantum_circuit.find_bit(c).index for c in instruction.clbits],
            dt=dt,
        )
    return gates.NativeCircuit(
        name=circuit.name,
        num_qubits=quantum_circuit.num_qubits,
        num_clbits=quantum_circuit.num_clbits,
        gates=tuple(native_gates),
    )


def _load_circuit(circuit: QasmCircuit) -> QuantumCircuit:
    """Parses the circuit's OpenQASM 3

    Raises:
        UnsupportedCircuitError: circuit {name} is not valid OpenQASM 3: {error}
    """
    try:
        return qasm3.loads(circuit.qasm)
    except Exception as exp:
        raise UnsupportedCircuitError(
            f"circuit {circuit.name} is not valid OpenQASM 3: {exp}"
        ) from exp


def _add_native_gates(
    native_gates: List[gates.Gate],
    instruction: CircuitInstruction,
    qubits: Sequence[int],
    clbits: Sequence[int],
    dt: float,
) -> None:
    """Appends the native gates of the instruction, which acts on the qubits and clbits,
    decomposing it by its definition where the table has none"""
    operation = instruction.operation
    name = operation.name
    if name == "measure":
        for qubit, clbit in zip(qubits, clbits):
            channel = sum(isinstance(gate, gates.Measure) for gate in native_gates)
            native_gates.append(
                gates.Measure(qubit=qubit, clbit=clbit, channel=channel)
            )
        return
    if name == "delay":
        seconds = _get_delay_seconds(operation, dt)
        native_gates.append(gates.Delay(qubits=tuple(qubits), duration=seconds))
        return
    if isinstance(operation, ControlFlowOp):
        raise UnsupportedCircuitError(f"gate {name} is not supported")

    params = _get_params(instruction)
    decomposition = _get_decomposition(name, qubits, params)
    if decomposition is not None:
        native_gates.extend(decomposition)
        return

    if operation.definition is None:
        raise UnsupportedCircuitError(f"gate {name} is not supported")
    _add_defined_gates(native_gates, operation.definition, qubits, clbits, dt)


def _get_delay_seconds(delay: Delay, dt: float) -> float:
    """Gets the delay's duration in seconds

    Raises:
        UnsupportedCircuitError: delay unit {unit} is not supported
    """
    if delay.unit == "dt":
        return float(delay.duration) * dt
    try:
        return float(delay.duration) * _SECONDS_PER_UNIT[delay.unit]
    except KeyError:
        raise UnsupportedCircuitError(f"delay unit {delay.unit} is not supported")


def _get_params(instruction: CircuitInstruction) -> List[float]:
    """Gets the instruction's parameters as numbers

    Raises:
        UnsupportedCircuitError: gate {name} has a parameter that is not a number
    """
    try:
        return [float(param) for param in instruction.operation.params]
    except TypeError as exp:
        raise UnsupportedCircuitError(
            f"gate {instruction.operation.name} has a parameter that is not a number"
        ) from exp


def _get_decomposition(
    name: str, qubits: Sequence[int], params: Sequence[float]
) -> List[gates.Gate] | None:
    """Gets the native gates of the standard gate of the name, up to a global phase, or
    None if the table has no such gate"""
    if name in ("rz", "p", "u1", "phase"):
        return [gates.Rz(qubits[0], params[0])]
    if name == "z":
        return [gates.Rz(qubits[0], math.pi)]
    if name == "s":
        return [gates.Rz(qubits[0], math.pi / 2)]
    if name == "sdg":
        return [gates.Rz(qubits[0], -math.pi / 2)]
    if name == "t":
        return [gates.Rz(qubits[0], math.pi / 4)]
    if name == "tdg":
        return [gates.Rz(qubits[0], -math.pi / 4)]
    if name == "rx":
        return [gates.Rxy(qubits[0], params[0], 0.0)]
    if name == "ry":
        return [gates.Rxy(qubits[0], params[0], math.pi / 2)]
    if name == "x":
        return [gates.Rxy(qubits[0], math.pi, 0.0)]
    if name == "y":
        return [gates.Rxy(qubits[0], math.pi, math.pi / 2)]
    if name == "sx":
        return [gates.Rxy(qubits[0], math.pi / 2, 0.0)]
    if name == "sxdg":
        return [gates.Rxy(qubits[0], -math.pi / 2, 0.0)]
    if name == "h":
        return _get_hadamard(qubits[0])
    if name in ("u", "U", "u3"):
        theta, phi, lam = params
        return [
            gates.Rz(qubits[0], lam),
            gates.Rxy(qubits[0], theta, math.pi / 2),
            gates.Rz(qubits[0], phi),
        ]
    if name in ("id", "i"):
        return []
    if name == "cz":
        return [gates.CZ(control=qubits[0], target=qubits[1])]
    if name in ("cx", "CX"):
        return _get_cnot(qubits[0], qubits[1])
    if name == "swap":
        first, second = qubits
        return (
            _get_cnot(first, second)
            + _get_cnot(second, first)
            + _get_cnot(first, second)
        )
    if name == "reset":
        return [gates.Reset(qubit) for qubit in qubits]
    if name == "barrier":
        return [gates.Barrier(qubits=tuple(qubits))]
    return None


def _add_defined_gates(
    native_gates: List[gates.Gate],
    definition: QuantumCircuit,
    qubits: Sequence[int],
    clbits: Sequence[int],
    dt: float,
) -> None:
    """Appends the native gates of the definition's instructions, its qubits and clbits
    being the given ones, in order"""
    for instruction in definition.data:
        _add_native_gates(
            native_gates,
            instruction,
            qubits=[qubits[definition.find_bit(q).index] for q in instruction.qubits],
            clbits=[clbits[definition.find_bit(c).index] for c in instruction.clbits],
            dt=dt,
        )


def _get_hadamard(qubit: int) -> List[gates.Gate]:
    """Gets the native gates of a Hadamard: Z, then a quarter turn about y"""
    return [gates.Rz(qubit, math.pi), gates.Rxy(qubit, math.pi / 2, math.pi / 2)]


def _get_cnot(control: int, target: int) -> List[gates.Gate]:
    """Gets the native gates of a CNOT: a CZ between Hadamards on the target"""
    return (
        _get_hadamard(target)
        + [gates.CZ(control=control, target=target)]
        + _get_hadamard(target)
    )
