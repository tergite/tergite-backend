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
"""The frames a device's pulse-level jobs may play on, and its gates' pulses as OpenPulse
defcals, which it publishes with its calibration"""
import math
import uuid
from typing import Any, Dict, Iterator, List, Sequence, Tuple

import numpy as np
from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice
from quantify_scheduler.helpers.waveforms import get_waveform
from quantify_scheduler.operations.gate_library import CZ, Rxy
from quantify_scheduler.operations.operation import Operation
from quantify_scheduler.schedules.schedule import ScheduleBase

from libs.device_parameters import BackendConfig, DeviceCalibration
from libs.device_parameters.dtos import GateDefcal, PulseFrame

from ..base import gates
from .device import (
    QBLOX_TIMEGRID_INTERVAL,
    apply_calibration,
    get_edge_names,
    get_element_name,
    get_frame_frequencies,
)

# the kind of frame that each suffix of a port, and of a clock, names
PORT_KINDS = {"mw": "drive", "res": "readout", "fl": "coupler"}
CLOCK_KINDS = {"01": "drive", "ro": "readout", "cz": "coupler"}
# what a published rz plays: the phase shift of quantify's Rz, as a cal block shifts it
RZ_BODY = "shift_phase({frame}, theta);"


def add_pulse_calibration(
    calibration: DeviceCalibration, backend_config: BackendConfig
) -> DeviceCalibration:
    """Adds to the calibration the frames the device's pulse-level jobs may play on and its
    gates' defcals, as its gate-level jobs play them; none if its backend config sets no
    pulse limits, as it then takes no pulse-level jobs

    Args:
        calibration: the device's calibration, of the backend config
        backend_config: the device's backend config

    Returns:
        the calibration, with the frames and defcals where the device takes pulse-level
        jobs
    """
    if backend_config.pulse_limits is None:
        return calibration
    frames, defcals = get_pulse_calibration(backend_config)
    return calibration.model_copy(update={"frames": frames, "defcals": defcals})


def get_pulse_calibration(
    backend_config: BackendConfig,
) -> Tuple[List[PulseFrame], List[GateDefcal]]:
    """Gets the frames a device's pulse-level jobs may play on, and the defcals of its x,
    sx, rz and cz, each playing exactly what a job's gate of that name plays

    A cz is given in both orders of its qubits, as a job's cz is compiled in either.
    measure is not given, as a pulse-level job may not define it.

    Args:
        backend_config: the device's backend config, with its calibration

    Returns:
        the frames, by name, and the defcals; none of either for a device without a
        calibration
    """
    if backend_config.calibration_config is None:
        return [], []
    # its own name, as QCoDeS names instruments for the whole process; its transmons and
    # edges are those the executor's device compiles against
    device = QuantumDevice(f"defcals_{uuid.uuid4().hex}")
    try:
        apply_calibration(device, backend_config)
        frequencies = get_frame_frequencies(device)
        qubit_numbers = backend_config.device_config.qubit_numbers
        dt = backend_config.general_config.dt
        frames = _get_frames(frequencies, qubit_numbers)
        defcals = list(_get_defcals(device, frequencies, qubit_numbers, dt))
    finally:
        device.close()
    return frames, defcals


def _get_frames(
    frequencies: Dict[str, float], qubit_numbers: Sequence[int]
) -> List[PulseFrame]:
    """Gets the frames of the calibrated frequencies, whose qubits are the device's"""
    frames = []
    for name, frequency in sorted(frequencies.items()):
        frame = gates.Frame.from_name(name)
        if not set(frame.qubit_ids) <= set(qubit_numbers):
            continue
        frames.append(
            PulseFrame(
                name=frame.name,
                kind=frame.kind,
                qubits=[qubit_numbers.index(qubit_id) for qubit_id in frame.qubit_ids],
                frequency=frequency,
            )
        )
    return frames


def _get_defcals(
    device: QuantumDevice,
    frequencies: Dict[str, float],
    qubit_numbers: Sequence[int],
    dt: float,
) -> Iterator[GateDefcal]:
    """Gets the defcals of each driven qubit's x, sx and rz, and each coupler's cz"""
    for index, qubit_id in enumerate(qubit_numbers):
        element = get_element_name(qubit_id)
        frame = f"{element}_drive"
        if frame not in frequencies:
            continue
        for gate, theta in (("x", 180), ("sx", 90)):
            body = _get_gate_body(device, Rxy(theta=theta, phi=0, qubit=element), dt)
            yield _get_defcal(gate, [index], body)
        yield _get_defcal("rz", [index], RZ_BODY.format(frame=frame), "angle theta")

    for name in get_edge_names(device):
        parent, child = (int(qubit.lstrip("q")) for qubit in name.split("_"))
        if not {parent, child} <= set(qubit_numbers):
            continue
        body = _get_gate_body(
            device, CZ(get_element_name(parent), get_element_name(child)), dt
        )
        indices = [qubit_numbers.index(parent), qubit_numbers.index(child)]
        yield _get_defcal("cz", indices, body)
        yield _get_defcal("cz", indices[::-1], body)


def _get_gate_body(device: QuantumDevice, operation: Operation, dt: float) -> str:
    """Gets the OpenPulse statements that play what the gate compiles to on the device

    Raises:
        ValueError: {pulse} has no OpenPulse counterpart in a defcal
        ValueError: {frame}'s operations overlap
    """
    schedule = Schedule("defcal")
    schedule.add(operation)
    compiled = SerialCompiler("defcal").compile(
        schedule, config=device.generate_compilation_config()
    )
    timed_pulses = sorted(_get_timed_pulses(compiled, 0.0), key=lambda v: v[0])
    return _to_openpulse(timed_pulses, duration=compiled.duration, dt=dt)


def _get_defcal(
    gate: str, qubits: List[int], body: str, parameter: str = ""
) -> GateDefcal:
    """Gets the defcal of the gate on the physical qubits, with the body's statements"""
    signature = f"{gate}({parameter})" if parameter else gate
    operands = ", ".join(f"${qubit}" for qubit in qubits)
    statements = "".join(f"    {line}\n" for line in body.splitlines())
    program = f"defcal {signature} {operands} {{\n{statements}}}"
    return GateDefcal(gate=gate, qubits=qubits, program=program)


def _get_timed_pulses(
    schedule: ScheduleBase, offset: float
) -> Iterator[Tuple[float, Dict[str, Any]]]:
    """Gets each pulse of the compiled schedule, those within it too, with its start in
    seconds

    Raises:
        ValueError: an acquisition has no OpenPulse counterpart in a defcal
    """
    for schedulable in schedule.schedulables.values():
        start = offset + schedulable["abs_time"]
        operation = schedule.operations[schedulable["operation_id"]]
        if isinstance(operation, ScheduleBase):
            yield from _get_timed_pulses(operation, start)
            continue
        if operation.data.get("acquisition_info"):
            raise ValueError("an acquisition has no OpenPulse counterpart in a defcal")
        for pulse in operation.data["pulse_info"]:
            yield start + pulse.get("t0", 0.0), pulse


def _to_openpulse(
    timed_pulses: List[Tuple[float, Dict[str, Any]]], duration: float, dt: float
) -> str:
    """Converts the timed pulses into OpenPulse statements that play them at those times,
    each frame running its own, as a cal block does: waveforms as declared samples,
    phase shifts as they are, and a wait where a frame idles

    Raises:
        ValueError: {pulse} has no OpenPulse counterpart in a defcal
        ValueError: {frame}'s operations overlap
    """
    declarations: List[str] = []
    statements: List[str] = []
    cursors: Dict[str, float] = {}
    for start, pulse in timed_pulses:
        if _is_idle(pulse):
            continue
        if pulse.get("wf_func") is not None and pulse.get("port"):
            frame = _get_port_frame(pulse["port"])
            samples = get_waveform(pulse, sampling_rate=1 / dt)
            name = f"w{len(declarations)}"
            declarations.append(f"waveform {name} = {_format_samples(samples)};")
            operation, length = f"play({frame}, {name});", len(samples) * dt
        elif "phase_shift" in pulse:
            frame = _get_clock_frame(pulse["clock"])
            radians = math.radians(pulse["phase_shift"])
            operation, length = (
                f"shift_phase({frame}, {radians!r});",
                QBLOX_TIMEGRID_INTERVAL,
            )
        else:
            raise ValueError(f"{pulse} has no OpenPulse counterpart in a defcal")
        statements.extend(_get_wait(frame, start - cursors.get(frame, 0.0), dt))
        statements.append(operation)
        cursors[frame] = start + length

    if cursors:
        last_frame = max(cursors, key=cursors.get)
        statements.extend(_get_wait(last_frame, duration - cursors[last_frame], dt))
    return "\n".join([*declarations, *statements])


def _is_idle(pulse: Dict[str, Any]) -> bool:
    """Whether the pulse only takes time, as an IdlePulse does"""
    return (
        pulse.get("wf_func") is None
        and pulse.get("port") is None
        and not (set(pulse) - {"wf_func", "t0", "duration", "clock", "port"})
    )


def _get_wait(frame: str, seconds: float, dt: float) -> List[str]:
    """Gets the delay of the frame for the seconds, none for no time

    Raises:
        ValueError: {frame}'s operations overlap
    """
    samples = round(seconds / dt)
    if samples < 0:
        raise ValueError(f"{frame}'s operations overlap")
    return [f"delay[{samples}dt] {frame};"] if samples else []


def _get_port_frame(port: str) -> str:
    """Gets the name of the frame that plays on the port, e.g. q12_drive of q12:mw"""
    element, suffix = port.split(":")
    return f"{element}_{PORT_KINDS[suffix]}"


def _get_clock_frame(clock: str) -> str:
    """Gets the name of the frame of the clock, e.g. q12_drive of q12.01"""
    element, suffix = clock.rsplit(".", 1)
    return f"{element}_{CLOCK_KINDS[suffix]}"


def _format_samples(samples: np.ndarray) -> str:
    """Formats the samples as an OpenQASM array literal, each exactly as it is"""
    return "{" + ", ".join(_format_complex(value) for value in samples) + "}"


def _format_complex(value: complex) -> str:
    """Formats the number as OpenQASM writes it, its imaginary part with im; exactly, as
    Python writes a float"""
    real, imag = float(np.real(value)), float(np.imag(value))
    if imag == 0:
        return repr(real)
    sign = "-" if imag < 0 else "+"
    return f"{real!r}{sign}{abs(imag)!r}im"
