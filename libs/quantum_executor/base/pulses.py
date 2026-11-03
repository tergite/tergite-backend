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
# The waveform generators are those OpenPulse names
# (https://openqasm.com/language/openpulse.html), with Amazon Braket's definitions.
"""Translates the OpenPulse of a cal block or a defcal into pulse operations on the frames
the device publishes"""
import math
from typing import Callable, Dict, List, Mapping, Sequence, Tuple

import numpy as np
from openpulse import ast

from utils.exc import UnsupportedCircuitError

from . import gates
from .expressions import get_complex, get_number, get_seconds

# the generators of waveforms, each a function of its sample times and its arguments, as
# get_waveform passes them: amplitudes complex, durations in seconds, the rest numbers
WAVEFORM_GENERATORS: Dict[str, Callable[..., np.ndarray]] = {
    "constant": lambda t, amp, d: np.full(t.shape, amp),
    "gaussian": lambda t, amp, d, sigma: amp
    * np.exp(-((t - d / 2) ** 2) / (2 * sigma**2)),
    "sech": lambda t, amp, d, sigma: amp / np.cosh((t - d / 2) / sigma),
    "gaussian_square": lambda t, amp, d, width, sigma: amp
    * np.exp(-(np.maximum(np.abs(t - d / 2) - width / 2, 0) ** 2) / (2 * sigma**2)),
    "drag": lambda t, amp, d, sigma, beta: amp
    * np.exp(-((t - d / 2) ** 2) / (2 * sigma**2))
    * (1 - 1j * beta * (t - d / 2) / sigma**2),
    "sine": lambda t, amp, d, frequency, phase: amp
    * np.sin(2 * math.pi * frequency * t + phase),
}
# what each argument of a generator is: an amplitude, a duration or a number
WAVEFORM_ARGUMENTS: Dict[str, Tuple[str, ...]] = {
    "constant": ("amplitude", "duration"),
    "gaussian": ("amplitude", "duration", "duration"),
    "sech": ("amplitude", "duration", "duration"),
    "gaussian_square": ("amplitude", "duration", "duration", "duration"),
    "drag": ("amplitude", "duration", "duration", "number"),
    "sine": ("amplitude", "duration", "number", "number"),
}
# the captures supported: capture_v1 integrates its frame's signal into a complex number
CAPTURES = ("capture_v1",)
FRAME_INSTRUCTIONS = (
    "play",
    "shift_phase",
    "set_phase",
    "shift_frequency",
    "set_frequency",
)


def get_pulse_block(
    statements: Sequence[ast.Statement],
    parameters: Mapping[str, complex],
    clbits: Mapping[str, List[int]],
    dt: float,
    first_channel: int,
) -> gates.PulseBlock:
    """Gets the pulse operations of the statements of a cal block or a defcal

    Frames are those the device publishes, named after their ports: q12_drive, q12_readout
    and q12_q13_coupler; a program declares none of its own. Waveforms are OpenPulse's
    generators, or arrays of samples, one per sample time. A capture is assigned to a
    classical bit, as c[0] = capture_v1(q12_readout, 2us), and acquired on the channel
    after the circuit's acquisitions before it.

    Args:
        statements: the block's statements
        parameters: the values of the defcal's parameters, by their names
        clbits: the program's classical bits, by their registers
        dt: the device's sample time in seconds
        first_channel: the channel of the block's first capture

    Returns:
        the block of pulse operations

    Raises:
        UnsupportedCircuitError: {statements} in a pulse-level block are not supported
        UnsupportedCircuitError: {name} is not a frame the device publishes ...
    """
    waveforms: Dict[str, Tuple[complex, ...]] = {}
    operations: List[gates.PulseOperation] = []
    for statement in statements:
        if isinstance(statement, ast.ExternDeclaration):
            _check_extern(statement)
        elif isinstance(statement, ast.ClassicalDeclaration):
            name, samples = _get_waveform_declaration(statement, parameters, dt)
            waveforms[name] = samples
        elif isinstance(statement, ast.ExpressionStatement):
            operations.append(
                _get_frame_instruction(statement.expression, parameters, waveforms, dt)
            )
        elif isinstance(statement, ast.ClassicalAssignment):
            channel = first_channel + sum(
                isinstance(op, gates.Capture) for op in operations
            )
            operations.append(_get_capture(statement, parameters, clbits, dt, channel))
        elif isinstance(statement, ast.DelayInstruction):
            duration = get_seconds(statement.duration, dt)
            operations.extend(
                gates.FrameDelay(frame=_get_frame(frame), duration=duration)
                for frame in statement.qubits
            )
        elif isinstance(statement, ast.QuantumBarrier):
            frames = tuple(_get_frame(frame) for frame in statement.qubits)
            operations.append(gates.FrameBarrier(frames=frames))
        else:
            raise UnsupportedCircuitError(
                f"{type(statement).__name__} in a pulse-level block is not supported"
            )
    return gates.PulseBlock(operations=tuple(operations))


def _check_extern(declaration: ast.ExternDeclaration) -> None:
    """Checks the extern declared is a waveform generator or a capture supported

    Raises:
        UnsupportedCircuitError: extern {name} is not supported
    """
    name = declaration.name.name
    if name not in WAVEFORM_GENERATORS and name not in CAPTURES:
        raise UnsupportedCircuitError(f"extern {name} is not supported")


def _get_waveform_declaration(
    declaration: ast.ClassicalDeclaration, parameters: Mapping[str, complex], dt: float
) -> Tuple[str, Tuple[complex, ...]]:
    """Gets the name and samples of a declared waveform

    Raises:
        UnsupportedCircuitError: frames other than the device's are not supported
        UnsupportedCircuitError: {kind} variables in a pulse-level block are not supported
    """
    kind = type(declaration.type).__name__
    if kind in ("FrameType", "PortType"):
        raise UnsupportedCircuitError(
            "frames other than the device's are not supported: play on q12_drive, "
            "q12_readout or q12_q13_coupler"
        )
    if kind != "WaveformType":
        raise UnsupportedCircuitError(
            f"{kind.removesuffix('Type').lower()} variables in a pulse-level block are "
            "not supported"
        )
    samples = _get_waveform(declaration.init_expression, parameters, {}, dt)
    return declaration.identifier.name, samples


def _get_frame_instruction(
    expression: ast.Expression,
    parameters: Mapping[str, complex],
    waveforms: Mapping[str, Tuple[complex, ...]],
    dt: float,
) -> gates.PulseOperation:
    """Gets the pulse operation of a call of play, or of the shift or setting of a frame's
    phase or frequency

    Raises:
        UnsupportedCircuitError: {name} in a pulse-level block is not supported
        UnsupportedCircuitError: {name} takes a frame and one argument
    """
    name = getattr(getattr(expression, "name", None), "name", type(expression).__name__)
    if name in CAPTURES:
        raise UnsupportedCircuitError(
            f"a {name} that keeps no result is not supported: assign it to a classical "
            "bit, as c[0] = capture_v1(q12_readout, 2us)"
        )
    if not isinstance(expression, ast.FunctionCall) or name not in FRAME_INSTRUCTIONS:
        raise UnsupportedCircuitError(f"{name} in a pulse-level block is not supported")
    if len(expression.arguments) != 2:
        raise UnsupportedCircuitError(f"{name} takes a frame and one argument")

    frame = _get_frame(expression.arguments[0])
    argument = expression.arguments[1]
    if name == "play":
        samples = _get_waveform(argument, parameters, waveforms, dt)
        return gates.Play(frame=frame, samples=samples)
    value = get_number(argument, parameters)
    if name == "shift_phase":
        return gates.ShiftPhase(frame=frame, phase=value)
    if name == "set_phase":
        return gates.SetPhase(frame=frame, phase=value)
    if name == "shift_frequency":
        return gates.ShiftFrequency(frame=frame, frequency=value)
    return gates.SetFrequency(frame=frame, frequency=value)


def _get_capture(
    assignment: ast.ClassicalAssignment,
    parameters: Mapping[str, complex],
    clbits: Mapping[str, List[int]],
    dt: float,
    channel: int,
) -> gates.Capture:
    """Gets the capture assigned to a classical bit

    Raises:
        UnsupportedCircuitError: only a capture may be assigned in a pulse-level block
        UnsupportedCircuitError: a capture is of a readout frame for a duration
    """
    capture = assignment.rvalue
    name = getattr(getattr(capture, "name", None), "name", None)
    if not isinstance(capture, ast.FunctionCall) or name not in CAPTURES:
        raise UnsupportedCircuitError(
            "only a capture, as capture_v1, may be assigned in a pulse-level block"
        )
    frame = _get_frame(capture.arguments[0]) if capture.arguments else None
    if frame is None or frame.kind != "readout" or len(capture.arguments) != 2:
        raise UnsupportedCircuitError(
            f"{name} takes a readout frame and a duration, as "
            "capture_v1(q12_readout, 2us)"
        )
    return gates.Capture(
        frame=frame,
        duration=get_seconds(capture.arguments[1], dt),
        clbit=_get_clbit(assignment.lvalue, clbits),
        channel=channel,
    )


def _get_waveform(
    expression: ast.Expression,
    parameters: Mapping[str, complex],
    waveforms: Mapping[str, Tuple[complex, ...]],
    dt: float,
) -> Tuple[complex, ...]:
    """Gets the samples of a waveform: one declared before, an array of samples, or a call
    of one of OpenPulse's generators, sampled at the middle of each sample time

    Raises:
        UnsupportedCircuitError: waveform {name} is not supported
        UnsupportedCircuitError: waveform {name} takes {n} arguments
        UnsupportedCircuitError: a waveform has samples of a magnitude beyond 1
    """
    if isinstance(expression, ast.Identifier) and expression.name in waveforms:
        return waveforms[expression.name]
    if isinstance(expression, ast.ArrayLiteral):
        samples = np.array(
            [get_complex(value, parameters) for value in expression.values]
        )
    else:
        samples = _get_generated_samples(expression, parameters, dt)
    if len(samples) == 0:
        raise UnsupportedCircuitError("a waveform of no samples is not supported")
    if np.max(np.abs(samples)) > 1 + 1e-9:
        raise UnsupportedCircuitError("a waveform has samples of a magnitude beyond 1")
    return tuple(complex(sample) for sample in samples)


def _get_generated_samples(
    call: ast.Expression, parameters: Mapping[str, complex], dt: float
) -> np.ndarray:
    """Gets the samples of a call of one of OpenPulse's waveform generators

    Raises:
        UnsupportedCircuitError: waveform {name} is not supported
        UnsupportedCircuitError: waveform {name} takes {n} arguments
    """
    name = getattr(getattr(call, "name", None), "name", type(call).__name__)
    if not isinstance(call, ast.FunctionCall) or name not in WAVEFORM_GENERATORS:
        raise UnsupportedCircuitError(f"waveform {name} is not supported")
    kinds = WAVEFORM_ARGUMENTS[name]
    if len(call.arguments) != len(kinds):
        raise UnsupportedCircuitError(f"waveform {name} takes {len(kinds)} arguments")

    arguments: List[complex | float] = []
    for kind, argument in zip(kinds, call.arguments):
        if kind == "amplitude":
            arguments.append(get_complex(argument, parameters))
        elif kind == "duration":
            arguments.append(get_seconds(argument, dt))
        else:
            arguments.append(get_number(argument, parameters))
    duration = arguments[1]
    times = (np.arange(round(duration / dt)) + 0.5) * dt
    return np.asarray(WAVEFORM_GENERATORS[name](times, *arguments), dtype=complex)


def _get_frame(expression: ast.Expression) -> gates.Frame:
    """Gets the device's frame the expression names

    Raises:
        UnsupportedCircuitError: {name} is not a frame the device publishes ...
    """
    name = getattr(expression, "name", type(expression).__name__)
    try:
        return gates.Frame.from_name(name)
    except ValueError as exp:
        raise UnsupportedCircuitError(
            f"{name} is not a frame the device publishes: they are named q12_drive, "
            "q12_readout and q12_q13_coupler"
        ) from exp


def _get_clbit(lvalue: ast.Expression, clbits: Mapping[str, List[int]]) -> int:
    """Gets the one classical bit the lvalue names

    Raises:
        UnsupportedCircuitError: a capture is assigned to one declared classical bit
    """
    identifier = lvalue.name if isinstance(lvalue, ast.IndexedIdentifier) else lvalue
    bits = clbits.get(getattr(identifier, "name", ""))
    try:
        if isinstance(lvalue, ast.IndexedIdentifier):
            [[index]] = lvalue.indices
            return bits[int(get_number(index, {}))]
        [bit] = bits
        return bit
    except (TypeError, ValueError, IndexError, UnsupportedCircuitError) as exp:
        raise UnsupportedCircuitError(
            "a capture is assigned to one declared classical bit"
        ) from exp
