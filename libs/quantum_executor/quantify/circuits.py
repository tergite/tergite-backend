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
# The schedule follows qpi-driver's `QuantifyExecutor._run_circuit`
# (https://github.com/sopherapps/qpi, MIT License, (C) Copyright Martin Ahindura 2026).
"""The quantify schedules of gate-level circuits, and of their pulse-level blocks"""

import math
from dataclasses import dataclass
from typing import Collection, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from quantify_scheduler import Schedule
from quantify_scheduler.operations.acquisition_library import SSBIntegrationComplex
from quantify_scheduler.operations.gate_library import CZ, Measure, Reset, Rxy, Rz
from quantify_scheduler.operations.operation import Operation
from quantify_scheduler.operations.pulse_library import (
    IdlePulse,
    NumericalPulse,
    ResetClockPhase,
    SetClockFrequency,
    ShiftClockPhase,
)
from quantify_scheduler.resources import ClockResource
from quantify_scheduler.schedules.schedule import Schedulable

from utils.exc import UnsupportedCircuitError

from ..base import gates
from ..base.quantum_job.dtos import NativeQobjConfig
from .device import QBLOX_TIMEGRID_INTERVAL, get_element_name
from .scheduling import get_clbit_name, mark_holds

# the seconds each shot idles first, for the qubits to relax
RELAXATION_DURATION = 500e-6


@dataclass(frozen=True)
class DeviceFrames:
    """What a circuit's pulse-level blocks are played with: the device's sample time, in
    seconds, and the calibrated frequency of each frame it publishes, by its name"""

    dt: float
    frequencies: Mapping[str, float]


def get_circuit_schedule(
    circuit: gates.NativeCircuit,
    native_config: NativeQobjConfig,
    element_names: Sequence[str],
    edge_names: Collection[str],
    calibrated_elements: Collection[str],
    frames: Optional[DeviceFrames] = None,
    mirrored_elements: Collection[str] = (),
) -> Schedule:
    """Gets the gate-level schedule of the circuit, for the device's transmons: its gates'
    operations in order, each marked with the transmons and classical bits it holds, which
    get_compilation_config's ALAP step times

    Each shot starts by letting every transmon relax for RELAXATION_DURATION. A barrier
    holds its qubits for no time, which lines them up; a pulse-level block holds the
    transmons of its frames and the bits it captures into. Each measurement is acquired on
    its gate's channel, with the protocol and bin mode of the native config.

    Args:
        circuit: the circuit of native gates
        native_config: the native config of the job, with its shots
        element_names: the name of the transmon of each of the circuit's qubits, in order
        edge_names: the names of the device's edges, each {parent}_{child}
        calibrated_elements: the names of the transmons the device has calibrated
        frames: the device's frames, which pulse-level blocks play on; none if it plays none
        mirrored_elements: the transmons whose drives turn phases the other way, as
            get_mirrored_elements gives them, on which the gates' and blocks' phases are
            negated

    Returns:
        the schedule of the circuit, repeated for each shot

    Raises:
        UnsupportedCircuitError: circuit {name} has {n} qubits, but the device has {m}
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
        UnsupportedCircuitError: qubit {name} is not calibrated
        UnsupportedCircuitError: frame {name} is not one of the device's: {names}
    """
    if circuit.num_qubits > len(element_names):
        raise UnsupportedCircuitError(
            f"circuit {circuit.name} has {circuit.num_qubits} qubits, "
            f"but the device has {len(element_names)}"
        )

    schedule = Schedule(circuit.name, repetitions=native_config.shots)
    relaxation = schedule.add(IdlePulse(duration=RELAXATION_DURATION))
    mark_holds(relaxation, element_names)
    frequencies = _reset_frame_frequencies(schedule, circuit, frames, start=relaxation)
    for gate in circuit.gates:
        holds = _get_holds(gate, element_names)
        operations = _get_operations(
            gate,
            native_config,
            element_names,
            edge_names,
            calibrated_elements,
            frames,
            frequencies,
            mirrored_elements,
        )
        for operation in operations:
            mark_holds(schedule.add(operation), holds)
    return schedule


def _reset_frame_frequencies(
    schedule: Schedule,
    circuit: gates.NativeCircuit,
    frames: Optional[DeviceFrames],
    start: Schedulable,
) -> Dict[gates.Frame, float]:
    """Sets the calibrated frequency, at the start of each shot, of each frame whose
    frequency the circuit's blocks change, as each run of an OpenPulse program starts with
    its frames anew; gets the frequency each such frame starts at

    Raises:
        UnsupportedCircuitError: frame {name} is not one of the device's: {names}
    """
    changed = {
        op.frame
        for gate in circuit.gates
        if isinstance(gate, gates.PulseBlock)
        for op in gate.operations
        if isinstance(op, (gates.ShiftFrequency, gates.SetFrequency))
    }
    frequencies = {}
    for frame in sorted(changed, key=lambda changed_frame: changed_frame.name):
        frequencies[frame] = _get_frame_frequency(frame, frames)
        _, clock = _get_port_clock(frame)
        schedule.add(
            SetClockFrequency(clock=clock, clock_freq_new=frequencies[frame]),
            ref_op=start,
            ref_pt="start",
        )
    return frequencies


def _get_holds(gate: gates.Gate, element_names: Sequence[str]) -> List[str]:
    """Gets what a gate holds: the transmons it plays on, and the classical bits it
    measures into"""
    if isinstance(gate, gates.PulseBlock):
        return _get_block_holds(gate)
    if isinstance(gate, (gates.Delay, gates.Barrier)):
        return [element_names[qubit] for qubit in gate.qubits]
    if isinstance(gate, gates.CZ):
        return [element_names[gate.control], element_names[gate.target]]
    if isinstance(gate, gates.Measure):
        return [element_names[gate.qubit], get_clbit_name(gate.clbit)]
    return [element_names[gate.qubit]]


def _get_block_holds(block: gates.PulseBlock) -> List[str]:
    """Gets what a pulse-level block holds: the transmons of the frames it plays on, a
    coupler's both, and the classical bits it captures into"""
    frames = set()
    clbits = set()
    for op in block.operations:
        if isinstance(op, gates.FrameBarrier):
            frames.update(op.frames)
            continue
        frames.add(op.frame)
        if isinstance(op, gates.Capture):
            clbits.add(get_clbit_name(op.clbit))
    elements = {
        get_element_name(qubit_id) for frame in frames for qubit_id in frame.qubit_ids
    }
    return sorted(elements | clbits)


def _get_operations(
    gate: gates.Gate,
    native_config: NativeQobjConfig,
    element_names: Sequence[str],
    edge_names: Collection[str],
    calibrated_elements: Collection[str],
    frames: Optional[DeviceFrames],
    frequencies: Dict[gates.Frame, float],
    mirrored_elements: Collection[str],
) -> List[Operation | Schedule]:
    """Gets the quantify operations of a native gate, in order; none for a delay of no
    duration. A rotation's phase on a mirrored transmon is negated

    Raises:
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
        UnsupportedCircuitError: qubit {name} is not calibrated
        UnsupportedCircuitError: frame {name} is not one of the device's: {names}
    """
    if isinstance(gate, gates.PulseBlock):
        return [
            _get_pulse_schedule(
                gate, native_config, frames, frequencies, mirrored_elements
            )
        ]
    if isinstance(gate, gates.Delay):
        duration = (
            round(gate.duration / QBLOX_TIMEGRID_INTERVAL) * QBLOX_TIMEGRID_INTERVAL
        )
        return [IdlePulse(duration=duration)] if duration > 0 else []
    if isinstance(gate, gates.Barrier):
        return [IdlePulse(duration=0)]
    if isinstance(gate, gates.CZ):
        return [_get_cz(gate, element_names, edge_names)]
    if not isinstance(gate, (gates.Measure, gates.Reset, gates.Rxy, gates.Rz)):
        return []

    element = element_names[gate.qubit]
    if element not in calibrated_elements:
        raise UnsupportedCircuitError(f"qubit {element} is not calibrated")
    sign = -1 if element in mirrored_elements else 1
    if isinstance(gate, gates.Measure):
        return [_get_measure(gate, element, native_config)]
    if isinstance(gate, gates.Reset):
        return [Reset(element)]
    if isinstance(gate, gates.Rxy):
        return [
            Rxy(
                theta=math.degrees(gate.theta),
                phi=sign * math.degrees(gate.phi),
                qubit=element,
            )
        ]
    # a Qblox sequencer updates a clock's phase at most once per grid interval
    return [
        Rz(theta=sign * math.degrees(gate.theta), qubit=element),
        IdlePulse(duration=QBLOX_TIMEGRID_INTERVAL),
    ]


def _get_pulse_schedule(
    block: gates.PulseBlock,
    native_config: NativeQobjConfig,
    frames: Optional[DeviceFrames],
    frequencies: Dict[gates.Frame, float],
    mirrored_elements: Collection[str],
) -> Schedule:
    """Gets the schedule of a pulse-level block, each frame's operations one after the
    other from its start, on the Qblox time grid; a phase or frequency update takes a grid
    interval, as a sequencer takes one at a time. The frequencies of the frames changed so
    far are updated, and a mirrored transmon's drive frame turns its phases the other way

    Raises:
        UnsupportedCircuitError: frame {name} is not one of the device's: {names}
    """
    schedule = Schedule("pulses")
    start = schedule.add(IdlePulse(duration=0))
    cursors: Dict[gates.Frame, float] = {}
    for op in block.operations:
        if isinstance(op, gates.FrameBarrier):
            aligned = op.frames or tuple(cursors)
            end = max((cursors.get(frame, 0.0) for frame in aligned), default=0.0)
            cursors.update({frame: end for frame in aligned})
            continue
        if op.frame.kind == "coupler":
            _add_coupler_clock(schedule, op.frame, frames)
        for operation, duration in _get_frame_operations(
            op, native_config, frames, frequencies, mirrored_elements
        ):
            at = cursors.get(op.frame, 0.0)
            if operation is not None:
                schedule.add(operation, ref_op=start, ref_pt="start", rel_time=at)
            cursors[op.frame] = at + _to_timegrid_after(duration)

    # what follows starts once every frame is done, its last update included
    end = max(cursors.values(), default=0.0)
    if end > 0:
        schedule.add(IdlePulse(duration=end), ref_op=start, ref_pt="start")
    return schedule


def _get_cz(
    gate: gates.CZ, element_names: Sequence[str], edge_names: Collection[str]
) -> CZ:
    """Gets the CZ on the edge between the gate's transmons, whose order it takes, as a
    CZ is the same either way

    Raises:
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
    """
    control = element_names[gate.control]
    target = element_names[gate.target]
    if f"{control}_{target}" in edge_names:
        return CZ(qC=control, qT=target)
    if f"{target}_{control}" in edge_names:
        return CZ(qC=target, qT=control)
    raise UnsupportedCircuitError(f"qubits {control} and {target} have no coupler")


def _get_measure(
    gate: gates.Measure, element: str, native_config: NativeQobjConfig
) -> Measure:
    """Gets the measurement of the transmon, acquired on the gate's channel"""
    return Measure(
        element,
        acq_channel=gate.channel,
        acq_index=0,
        acq_protocol=native_config.protocol.value,
        bin_mode=native_config.bin_mode.value,
    )


def _get_frame_operations(
    op: gates.PulseOperation,
    native_config: NativeQobjConfig,
    frames: Optional[DeviceFrames],
    frequencies: Dict[gates.Frame, float],
    mirrored_elements: Collection[str],
) -> List[Tuple[Optional[Operation], float]]:
    """Gets the quantify operations of a pulse operation on a frame, each with the time it
    takes of the frame, none for a wait; a frequency changed is kept in the frequencies

    Raises:
        UnsupportedCircuitError: frame {name} is not one of the device's: {names}
    """
    frequency = _get_frame_frequency(op.frame, frames)
    port, clock = _get_port_clock(op.frame)
    if isinstance(op, gates.Play):
        pulse = _get_numerical_pulse(op, port, clock, frames.dt)
        return [(pulse, len(op.samples) * frames.dt)]
    sign = -1 if _is_mirrored(op.frame, mirrored_elements) else 1
    # a played envelope is not conjugated on a mirrored drive: a gate's is not either,
    # its motzoi calibrated as it plays
    if isinstance(op, gates.ShiftPhase):
        shift = ShiftClockPhase(phase_shift=sign * math.degrees(op.phase), clock=clock)
        return [(shift, QBLOX_TIMEGRID_INTERVAL)]
    if isinstance(op, gates.SetPhase):
        shift = ShiftClockPhase(phase_shift=sign * math.degrees(op.phase), clock=clock)
        return [
            (ResetClockPhase(clock=clock), QBLOX_TIMEGRID_INTERVAL),
            (shift, QBLOX_TIMEGRID_INTERVAL),
        ]
    if isinstance(op, (gates.ShiftFrequency, gates.SetFrequency)):
        current = frequencies.get(op.frame, frequency)
        if isinstance(op, gates.ShiftFrequency):
            frequencies[op.frame] = current + op.frequency
        else:
            frequencies[op.frame] = op.frequency
        update = SetClockFrequency(clock=clock, clock_freq_new=frequencies[op.frame])
        return [(update, QBLOX_TIMEGRID_INTERVAL)]
    if isinstance(op, gates.FrameDelay):
        return [(None, op.duration)]
    acquisition = SSBIntegrationComplex(
        port=port,
        clock=clock,
        duration=_to_timegrid_after(op.duration),
        acq_channel=op.channel,
        acq_index=0,
        bin_mode=native_config.bin_mode.value,
    )
    return [(acquisition, op.duration)]


def _add_coupler_clock(
    schedule: Schedule, frame: gates.Frame, frames: Optional[DeviceFrames]
) -> None:
    """Adds the coupler's clock at its calibrated frequency, once, as the device's
    transmons, which define the clocks of drives and readouts, define none for it"""
    _, clock = _get_port_clock(frame)
    if clock not in schedule.resources:
        frequency = _get_frame_frequency(frame, frames)
        schedule.add_resource(ClockResource(name=clock, freq=frequency))


def _get_numerical_pulse(
    play: gates.Play, port: str, clock: str, dt: float
) -> NumericalPulse:
    """Gets the pulse of the played samples, one per sample time, the last held to the
    end of its time, as a numerical pulse lasts from its first sample time to its last
    """
    samples = np.array([*play.samples, play.samples[-1]])
    return NumericalPulse(
        samples=samples,
        t_samples=np.arange(len(samples)) * dt,
        port=port,
        clock=clock,
    )


def _get_frame_frequency(frame: gates.Frame, frames: Optional[DeviceFrames]) -> float:
    """Gets the calibrated frequency of the device's frame

    Raises:
        UnsupportedCircuitError: frame {name} is not one of the device's: {names}
    """
    if frames is None or frame.name not in frames.frequencies:
        names = ", ".join(sorted(frames.frequencies)) if frames else "none"
        raise UnsupportedCircuitError(
            f"frame {frame.name} is not one of the device's: {names}"
        )
    return frames.frequencies[frame.name]


def _is_mirrored(frame: gates.Frame, mirrored_elements: Collection[str]) -> bool:
    """Whether the frame is the drive of a mirrored transmon"""
    return frame.kind == "drive" and _get_element(frame) in mirrored_elements


def _get_element(frame: gates.Frame) -> str:
    """Gets the name of the transmon, or coupler edge, the frame is of"""
    return frame.name.removesuffix(f"_{frame.kind}")


def _get_port_clock(frame: gates.Frame) -> Tuple[str, str]:
    """Gets the port and clock of the frame, as the hardware config names them"""
    element = _get_element(frame)
    if frame.kind == "drive":
        return f"{element}:mw", f"{element}.01"
    if frame.kind == "readout":
        return f"{element}:res", f"{element}.ro"
    return f"{element}:fl", f"{element}.cz"


def _to_timegrid_after(seconds: float) -> float:
    """Gets the first time on the Qblox time grid at or after the given one, in seconds,
    so that what follows on a frame starts on the grid"""
    return math.ceil(seconds / QBLOX_TIMEGRID_INTERVAL - 1e-9) * QBLOX_TIMEGRID_INTERVAL
