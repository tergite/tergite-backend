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
# The standard gate definitions are those of OpenQASM's stdgates.inc
# (https://github.com/openqasm/openqasm, Apache License 2.0).
"""Translates the OpenQASM 3 circuits of gate-level jobs into native gates"""

import functools
import math
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Tuple

import openpulse
from antlr4 import CommonTokenStream, InputStream, Recognizer, Token
from antlr4.error.ErrorListener import ErrorListener
from antlr4.error.Errors import RecognitionException
from openpulse import ast
from openqasm3._antlr.qasm3Lexer import qasm3Lexer
from openqasm3._antlr.qasm3Parser import qasm3Parser

from utils.exc import UnsupportedCircuitError

from . import gates
from .circuits import QasmCircuit
from .expressions import get_number, get_seconds
from .pulses import get_pulse_block

# the statements refused by name, which their AST classes do not say plainly
_STATEMENT_NAMES = {
    "BranchingStatement": "if statements",
    "ForInLoop": "for loops",
    "WhileLoop": "while loops",
    "SwitchStatement": "switch statements",
    "SubroutineDefinition": "subroutines",
    "IODeclaration": "inputs and outputs",
}
# the gates of stdgates.inc that the table of native decompositions has not, as it defines
# them, with U for its u1 and u3
STANDARD_GATE_DEFINITIONS = """
OPENQASM 3.0;
gate cy a, b { sdg b; cx a, b; s b; }
gate cp(lam) a, b { p(lam/2) a; cx a, b; p(-lam/2) b; cx a, b; p(lam/2) b; }
gate cphase(lam) a, b { cp(lam) a, b; }
gate crx(theta) a, b {
    p(pi/2) b; cx a, b; U(-theta/2, 0, 0) b; cx a, b; U(theta/2, -pi/2, 0) b;
}
gate cry(theta) a, b { ry(theta/2) b; cx a, b; ry(-theta/2) b; cx a, b; }
gate crz(theta) a, b { rz(theta/2) b; cx a, b; rz(-theta/2) b; cx a, b; }
gate ch a, b { s b; h b; t b; cx a, b; tdg b; h b; sdg b; }
gate ccx a, b, c {
    h c; cx b, c; tdg c; cx a, c; t c; cx b, c; tdg c; cx a, c; t b; t c; h c;
    cx a, b; t a; tdg b; cx a, b;
}
gate cswap a, b, c { cx c, b; ccx a, b, c; cx c, b; }
gate cu(theta, phi, lam, gamma) c, t {
    p(gamma) c; p((lam + phi)/2) c; p((lam - phi)/2) t; cx c, t;
    U(-theta/2, 0, -(phi + lam)/2) t; cx c, t; U(theta/2, phi, 0) t;
}
gate u2(phi, lam) q { U(pi/2, phi, lam) q; }
"""


@dataclass
class _Scope:
    """What a program has declared and translated so far, as its statements are read"""

    dt: float
    num_qubits: int
    qubits: Dict[str, List[int]] = field(default_factory=dict)
    clbits: Dict[str, List[int]] = field(default_factory=dict)
    definitions: Dict[str, ast.QuantumGateDefinition] = field(default_factory=dict)
    # each gate's defcals, by its name and physical qubits
    defcals: Dict[Tuple[str, Tuple[int, ...]], List[ast.CalibrationDefinition]] = field(
        default_factory=dict
    )
    native_gates: List[gates.Gate] = field(default_factory=list)


def to_native_circuit(circuit: QasmCircuit, dt: float) -> gates.NativeCircuit:
    """Parses the circuit's OpenQASM 3 and translates its gates into native gates

    Gates beyond the native ones are decomposed into them, up to a global phase: those of
    OpenQASM's standard library that a table names, the rest of it and any gate the program
    defines by its definition. A gate a defcal of the program matches, by its name, its
    physical qubits and its parameters, is that defcal's pulses instead, and a cal block is
    its own pulses (pulses.py). The qubits are physical, as $2, or declared, numbered in
    the order of their declarations; the classical bits are numbered likewise. Each
    measurement, and each capture, is acquired on its own channel, numbered in the order
    the circuit acquires.

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
        UnsupportedCircuitError: unit {unit} is not supported
        UnsupportedCircuitError: {statements} are not supported
    """
    program = _parse(circuit)
    scope = _Scope(dt=dt, num_qubits=_get_num_qubits(program))
    for statement in program.statements:
        _add_statement(scope, statement)

    num_clbits = sum(len(bits) for bits in scope.clbits.values())
    if num_clbits != circuit.num_clbits:
        raise UnsupportedCircuitError(
            f"circuit {circuit.name} declares {circuit.num_clbits} classical bits, "
            f"but its OpenQASM has {num_clbits}"
        )
    return gates.NativeCircuit(
        name=circuit.name,
        num_qubits=scope.num_qubits,
        num_clbits=num_clbits,
        gates=tuple(scope.native_gates),
    )


def _parse(circuit: QasmCircuit) -> ast.Program:
    """Parses the circuit's OpenQASM 3, which may hold OpenPulse

    Raises:
        UnsupportedCircuitError: circuit {name} is not valid OpenQASM 3: {error}
    """
    try:
        return openpulse.parse(circuit.qasm)
    except Exception as exp:
        error = _get_syntax_error(circuit.qasm) or f"{exp}"
        raise UnsupportedCircuitError(
            f"circuit {circuit.name} is not valid OpenQASM 3: {error}"
        ) from exp


def _get_syntax_error(qasm: str) -> Optional[str]:
    """The first syntax error of the OpenQASM 3 outside its cal blocks, as where it is
    and what, or None if it has none; openqasm3's own parse raises without saying it"""
    listener = _SyntaxErrorListener()
    lexer = qasm3Lexer(InputStream(qasm))
    lexer.removeErrorListeners()
    lexer.addErrorListener(listener)
    parser = qasm3Parser(CommonTokenStream(lexer))
    parser.removeErrorListeners()
    parser.addErrorListener(listener)
    parser.program()
    return listener.errors[0] if listener.errors else None


class _SyntaxErrorListener(ErrorListener):
    """Keeps the syntax errors ANTLR reports, which it would otherwise print"""

    def __init__(self) -> None:
        super().__init__()
        self.errors: List[str] = []

    def syntaxError(
        self,
        recognizer: Recognizer,
        offendingSymbol: Token,
        line: int,
        column: int,
        msg: str,
        e: Optional[RecognitionException],
    ) -> None:
        """Keeps the error, as where it is and what"""
        self.errors.append(f"line {line}, column {column}: {msg}")


def _get_num_qubits(program: ast.Program) -> int:
    """The number of the program's qubits: its declared ones, or one past the highest
    physical qubit it names"""
    declared = sum(
        _get_size(statement.size)
        for statement in program.statements
        if isinstance(statement, ast.QubitDeclaration)
    )
    physical = [
        int(node.name[1:]) + 1
        for node in _get_nodes(program)
        if isinstance(node, ast.Identifier) and node.name.startswith("$")
    ]
    return max([declared, *physical])


def _add_statement(scope: _Scope, statement: ast.Statement) -> None:
    """Translates the top-level statement into the scope

    Raises:
        UnsupportedCircuitError: {statements} are not supported
    """
    if isinstance(statement, ast.Include):
        if statement.filename != "stdgates.inc":
            raise UnsupportedCircuitError(
                f"include {statement.filename} is not supported"
            )
    elif isinstance(statement, ast.CalibrationGrammarDeclaration):
        if statement.name != "openpulse":
            raise UnsupportedCircuitError(
                f"calibration grammar {statement.name} is not supported"
            )
    elif isinstance(statement, ast.CalibrationStatement):
        scope.native_gates.append(
            get_pulse_block(
                statement.body,
                parameters={},
                clbits=scope.clbits,
                dt=scope.dt,
                first_channel=_count_acquisitions(scope),
            )
        )
    elif isinstance(statement, ast.CalibrationDefinition):
        _add_defcal(scope, statement)
    elif isinstance(statement, ast.QubitDeclaration):
        _add_register(scope.qubits, statement.qubit.name, _get_size(statement.size))
    elif isinstance(statement, ast.ClassicalDeclaration):
        _add_clbit_register(scope, statement)
    elif isinstance(statement, ast.QuantumGateDefinition):
        scope.definitions[statement.name.name] = statement
    elif isinstance(statement, (ast.QuantumGate, ast.QuantumPhase)):
        _add_gate(
            scope, statement, parameters={}, local_qubits={}, defining=frozenset()
        )
    elif isinstance(statement, ast.QuantumMeasurementStatement):
        _add_measurement(scope, statement)
    elif isinstance(statement, ast.QuantumReset):
        for qubit in _get_qubits(scope, statement.qubits, local_qubits={}):
            scope.native_gates.append(gates.Reset(qubit))
    elif isinstance(statement, ast.QuantumBarrier):
        qubits = _get_operand_qubits(scope, statement.qubits)
        scope.native_gates.append(gates.Barrier(qubits=tuple(qubits)))
    elif isinstance(statement, ast.DelayInstruction):
        seconds = get_seconds(statement.duration, scope.dt)
        for qubit in _get_operand_qubits(scope, statement.qubits):
            scope.native_gates.append(gates.Delay(qubits=(qubit,), duration=seconds))
    else:
        name = type(statement).__name__
        raise UnsupportedCircuitError(
            f"{_STATEMENT_NAMES.get(name, name)} are not supported"
        )


def _add_register(registers: Dict[str, List[int]], name: str, size: int) -> None:
    """Adds the register of the name and size, its bits numbered after those before"""
    first = sum(len(bits) for bits in registers.values())
    registers[name] = list(range(first, first + size))


def _add_clbit_register(scope: _Scope, declaration: ast.ClassicalDeclaration) -> None:
    """Adds the register of classical bits the declaration declares

    Raises:
        UnsupportedCircuitError: classical {type} variables are not supported
    """
    if not isinstance(declaration.type, ast.BitType):
        kind = type(declaration.type).__name__.removesuffix("Type").lower()
        raise UnsupportedCircuitError(f"classical {kind} variables are not supported")
    _add_register(
        scope.clbits, declaration.identifier.name, _get_size(declaration.type.size)
    )


def _add_defcal(scope: _Scope, defcal: ast.CalibrationDefinition) -> None:
    """Keeps the defcal, for the gates it matches

    Raises:
        UnsupportedCircuitError: a defcal of other than physical qubits is not supported
        UnsupportedCircuitError: a defcal with a result, as of measure, is not supported
    """
    if not all(
        isinstance(qubit, ast.Identifier) and qubit.name.startswith("$")
        for qubit in defcal.qubits
    ):
        raise UnsupportedCircuitError(
            "a defcal of other than physical qubits, as $0, is not supported"
        )
    if defcal.return_type is not None:
        raise UnsupportedCircuitError(
            "a defcal with a result, as of measure, is not supported"
        )
    qubits = tuple(int(qubit.name[1:]) for qubit in defcal.qubits)
    scope.defcals.setdefault((defcal.name.name, qubits), []).append(defcal)


def _add_gate(
    scope: _Scope,
    gate: ast.QuantumStatement,
    parameters: Mapping[str, float],
    local_qubits: Mapping[str, List[int]],
    defining: FrozenSet[str],
) -> None:
    """Appends the native gates of the gate, or of a global phase, which is none; within a
    definition, its parameters and qubits are those named, and the gates being defined
    those of the names

    Raises:
        UnsupportedCircuitError: gate {name} is not supported
        UnsupportedCircuitError: gate {name} has a parameter that is not a number
    """
    if gate.modifiers:
        raise UnsupportedCircuitError(
            f"gate modifiers, as {gate.modifiers[0].modifier.name} @, are not supported"
        )
    if isinstance(gate, ast.QuantumPhase):
        return
    if not isinstance(gate, ast.QuantumGate):
        raise UnsupportedCircuitError(
            f"{type(gate).__name__} in a gate is not supported"
        )
    name = gate.name.name
    if gate.duration is not None:
        raise UnsupportedCircuitError(f"gate {name} with a duration is not supported")
    try:
        params = [get_number(argument, parameters) for argument in gate.arguments]
    except UnsupportedCircuitError as exp:
        raise UnsupportedCircuitError(
            f"gate {name} has a parameter that is not a number"
        ) from exp

    operands = [_get_qubits(scope, qubit, local_qubits) for qubit in gate.qubits]
    for qubits in _broadcast(operands):
        defcal_parameters = _match_defcal(scope, name, qubits, params)
        if defcal_parameters is not None:
            defcal, parameters_by_name = defcal_parameters
            scope.native_gates.append(
                get_pulse_block(
                    defcal.body,
                    parameters=parameters_by_name,
                    clbits=scope.clbits,
                    dt=scope.dt,
                    first_channel=_count_acquisitions(scope),
                )
            )
            continue
        decomposition = _get_decomposition(name, qubits, params)
        if decomposition is not None:
            scope.native_gates.extend(decomposition)
            continue
        definition = scope.definitions.get(name) or _get_standard_definitions().get(
            name
        )
        if definition is None or name in defining:
            raise UnsupportedCircuitError(f"gate {name} is not supported")
        _add_defined_gates(scope, definition, qubits, params, defining | {name})


def _match_defcal(
    scope: _Scope, name: str, qubits: Sequence[int], params: Sequence[float]
) -> Optional[Tuple[ast.CalibrationDefinition, Dict[str, complex]]]:
    """The last defcal of the gate on the qubits whose parameters match, as a parameter
    it names takes any value and one it writes as a number only that, with the values of
    its named parameters; None if none matches"""
    for defcal in reversed(scope.defcals.get((name, tuple(qubits)), [])):
        if len(defcal.arguments) != len(params):
            continue
        parameters: Dict[str, complex] = {}
        for argument, value in zip(defcal.arguments, params):
            if isinstance(argument, ast.ClassicalArgument):
                parameters[argument.name.name] = value
            elif abs(get_number(argument, {}) - value) > 1e-9:
                break
        else:
            return defcal, parameters
    return None


def _count_acquisitions(scope: _Scope) -> int:
    """The number of the measurements and captures translated so far, which is the
    channel of the next"""
    measures = sum(isinstance(gate, gates.Measure) for gate in scope.native_gates)
    captures = sum(
        isinstance(op, gates.Capture)
        for gate in scope.native_gates
        if isinstance(gate, gates.PulseBlock)
        for op in gate.operations
    )
    return measures + captures


def _add_defined_gates(
    scope: _Scope,
    definition: ast.QuantumGateDefinition,
    qubits: Sequence[int],
    params: Sequence[float],
    defining: FrozenSet[str],
) -> None:
    """Appends the native gates of the definition's body, its parameters and qubits being
    the given ones, in order

    Raises:
        UnsupportedCircuitError: gate {name} takes {n} parameters and {m} qubits
    """
    name = definition.name.name
    if len(params) != len(definition.arguments) or len(qubits) != len(
        definition.qubits
    ):
        raise UnsupportedCircuitError(
            f"gate {name} takes {len(definition.arguments)} parameters and "
            f"{len(definition.qubits)} qubits"
        )
    parameters = {arg.name: value for arg, value in zip(definition.arguments, params)}
    local_qubits = {arg.name: [qubit] for arg, qubit in zip(definition.qubits, qubits)}
    for statement in definition.body:
        _add_gate(scope, statement, parameters, local_qubits, defining)


def _add_measurement(scope: _Scope, statement: ast.QuantumMeasurementStatement) -> None:
    """Appends a measurement of each qubit measured into its classical bit, each acquired
    on the next channel

    Raises:
        UnsupportedCircuitError: a measurement that keeps no result is not supported
        UnsupportedCircuitError: a measurement of {n} qubits into {m} classical bits is not
            supported
    """
    if statement.target is None:
        raise UnsupportedCircuitError(
            "a measurement that keeps no result is not supported"
        )
    qubits = _get_qubits(scope, statement.measure.qubit, local_qubits={})
    clbits = _get_bits(scope.clbits, statement.target, "classical bit")
    if len(qubits) != len(clbits):
        raise UnsupportedCircuitError(
            f"a measurement of {len(qubits)} qubits into {len(clbits)} classical bits "
            "is not supported"
        )
    for qubit, clbit in zip(qubits, clbits):
        channel = _count_acquisitions(scope)
        scope.native_gates.append(
            gates.Measure(qubit=qubit, clbit=clbit, channel=channel)
        )


def _get_operand_qubits(scope: _Scope, operands: Sequence[ast.Expression]) -> List[int]:
    """The qubits of the operands, or every qubit where there are none"""
    if not operands:
        return list(range(scope.num_qubits))
    return [
        qubit
        for operand in operands
        for qubit in _get_qubits(scope, operand, local_qubits={})
    ]


def _get_qubits(
    scope: _Scope, operand: ast.Expression, local_qubits: Mapping[str, List[int]]
) -> List[int]:
    """The qubits the operand names: a physical qubit, a qubit of a definition, or a
    declared register or one of its qubits

    Raises:
        UnsupportedCircuitError: qubit {name} is not declared
    """
    if isinstance(operand, ast.Identifier) and operand.name.startswith("$"):
        return [int(operand.name[1:])]
    if isinstance(operand, ast.Identifier) and operand.name in local_qubits:
        return list(local_qubits[operand.name])
    return _get_bits(scope.qubits, operand, "qubit")


def _get_bits(
    registers: Mapping[str, List[int]], operand: ast.Expression, kind: str
) -> List[int]:
    """The bits of the register the operand names, or the one bit its index names

    Raises:
        UnsupportedCircuitError: {kind} {name} is not declared
        UnsupportedCircuitError: {kind} {name} is indexed by other than one number
    """
    identifier = operand.name if isinstance(operand, ast.IndexedIdentifier) else operand
    name = getattr(identifier, "name", type(operand).__name__)
    if name not in registers:
        raise UnsupportedCircuitError(f"{kind} {name} is not declared")
    bits = registers[name]
    if isinstance(operand, ast.Identifier):
        return list(bits)
    try:
        [[index_expression]] = operand.indices
        return [bits[int(get_number(index_expression, {}))]]
    except (ValueError, TypeError, IndexError, UnsupportedCircuitError) as exp:
        raise UnsupportedCircuitError(
            f"{kind} {name} is indexed by other than one number of its own"
        ) from exp


def _broadcast(operands: List[List[int]]) -> Iterator[List[int]]:
    """The qubits of each application of a gate to the operands: once if each names one
    qubit, else once for each qubit of the registers, which must be as long

    Raises:
        UnsupportedCircuitError: a gate on registers of different sizes is not supported
    """
    sizes = {len(qubits) for qubits in operands if len(qubits) != 1}
    if len(sizes) > 1:
        raise UnsupportedCircuitError(
            "a gate on registers of different sizes is not supported"
        )
    size = sizes.pop() if sizes else 1
    for index in range(size):
        yield [qubits[index] if len(qubits) > 1 else qubits[0] for qubits in operands]


def _get_size(size: ast.Expression | None) -> int:
    """The size of a register as declared, one if it states none"""
    return 1 if size is None else int(get_number(size, {}))


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
    return None


@functools.cache
def _get_standard_definitions() -> Dict[str, ast.QuantumGateDefinition]:
    """The definitions of the standard gates the table has not, by their names"""
    program = openpulse.parse(STANDARD_GATE_DEFINITIONS)
    return {
        statement.name.name: statement
        for statement in program.statements
        if isinstance(statement, ast.QuantumGateDefinition)
    }


def _get_nodes(node: object) -> Iterator[object]:
    """The node and every node of the syntax tree below it"""
    yield node
    if is_dataclass(node):
        for child in (getattr(node, f.name) for f in fields(node)):
            yield from _get_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from _get_nodes(child)


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
