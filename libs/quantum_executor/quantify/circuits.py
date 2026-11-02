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
"""The quantify schedules of gate-level circuits"""
import math
from typing import Collection, List, Mapping, Optional, Sequence, Set

from quantify_scheduler import Schedule
from quantify_scheduler.operations.gate_library import CZ, Measure, Reset, Rxy, Rz
from quantify_scheduler.operations.operation import Operation
from quantify_scheduler.operations.pulse_library import IdlePulse
from quantify_scheduler.schedules.schedule import Schedulable

from utils.exc import UnsupportedCircuitError

from ..base import gates
from ..base.quantum_job.dtos import NativeQobjConfig
from .device import QBLOX_TIMEGRID_INTERVAL
from .instruction import INITIAL_RESET_VALUE


def get_circuit_schedule(
    circuit: gates.NativeCircuit,
    native_config: NativeQobjConfig,
    element_names: Sequence[str],
    edge_names: Collection[str],
    measure_durations: Mapping[str, float],
) -> Schedule:
    """Gets the gate-level schedule of the circuit, for the device's transmons

    Each shot starts after an idle as long as the qobj path's, in which the qubits relax.
    The operations run one after the other, but for consecutive measurements of distinct
    qubits, which start together; what follows them waits for the longest. Each
    measurement is acquired on its gate's channel, with the protocol and bin mode of the
    native config.

    Args:
        circuit: the circuit of native gates
        native_config: the native config of the job, with its shots
        element_names: the name of the transmon of each of the circuit's qubits, in order
        edge_names: the names of the device's edges, each {parent}_{child}
        measure_durations: the seconds each transmon's measurement takes, by its name

    Returns:
        the schedule of the circuit, repeated for each shot

    Raises:
        UnsupportedCircuitError: circuit {name} has {n} qubits, but the device has {m}
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
        UnsupportedCircuitError: qubit {name} is not calibrated
    """
    if circuit.num_qubits > len(element_names):
        raise UnsupportedCircuitError(
            f"circuit {circuit.name} has {circuit.num_qubits} qubits, "
            f"but the device has {len(element_names)}"
        )

    schedule = Schedule(circuit.name, repetitions=native_config.shots)
    # the schedulable the next operation follows; None follows the last one added
    after: Optional[Schedulable] = schedule.add(IdlePulse(duration=INITIAL_RESET_VALUE))
    after_duration = 0.0
    group_start: Optional[Schedulable] = None
    group_qubits: Set[int] = set()
    for gate in circuit.gates:
        if isinstance(gate, gates.Measure):
            element = element_names[gate.qubit]
            duration = _get_measure_duration(element, measure_durations)
            measure = _get_measure(gate, element, native_config)
            if group_start is not None and gate.qubit not in group_qubits:
                schedulable = schedule.add(measure, ref_op=group_start, ref_pt="start")
                group_qubits.add(gate.qubit)
                if duration > after_duration:
                    after, after_duration = schedulable, duration
            else:
                group_start = after = schedule.add(measure, ref_op=after)
                group_qubits, after_duration = {gate.qubit}, duration
            continue

        group_start, group_qubits = None, set()
        for operation in _get_operations(gate, element_names, edge_names):
            schedule.add(operation, ref_op=after)
            after = None
    return schedule


def get_measure_duration(
    pulse_duration: float, acq_delay: float, integration_time: float
) -> float:
    """Gets the seconds a transmon's measurement takes: its pulse, or its acquisition
    after the delay, whichever ends later

    Args:
        pulse_duration: the duration of the readout pulse, in seconds
        acq_delay: the delay of the acquisition from the pulse's start, in seconds
        integration_time: the duration of the acquisition, in seconds

    Returns:
        the duration of the measurement
    """
    return max(pulse_duration, acq_delay + integration_time)


def _get_measure_duration(
    element: str, measure_durations: Mapping[str, float]
) -> float:
    """Gets the seconds the transmon's measurement takes

    Raises:
        UnsupportedCircuitError: qubit {name} is not calibrated
    """
    try:
        return measure_durations[element]
    except KeyError:
        raise UnsupportedCircuitError(f"qubit {element} is not calibrated")


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


def _get_operations(
    gate: gates.Gate, element_names: Sequence[str], edge_names: Collection[str]
) -> List[Operation]:
    """Gets the quantify operations of a native gate but a measurement; a barrier has
    none, as the operations run one after the other

    Raises:
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
    """
    if isinstance(gate, gates.Rxy):
        return [
            Rxy(
                theta=math.degrees(gate.theta),
                phi=math.degrees(gate.phi),
                qubit=element_names[gate.qubit],
            )
        ]
    if isinstance(gate, gates.Rz):
        # a Qblox sequencer updates a clock's phase at most once per grid interval
        return [
            Rz(theta=math.degrees(gate.theta), qubit=element_names[gate.qubit]),
            IdlePulse(duration=QBLOX_TIMEGRID_INTERVAL),
        ]
    if isinstance(gate, gates.CZ):
        return [_get_cz(gate, element_names, edge_names)]
    if isinstance(gate, gates.Reset):
        return [Reset(element_names[gate.qubit])]
    if isinstance(gate, gates.Delay):
        duration = _to_timegrid(gate.duration)
        return [IdlePulse(duration=duration)] if duration > 0 else []
    return []


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


def _to_timegrid(seconds: float) -> float:
    """Gets the time on the Qblox time grid nearest the given one, in seconds"""
    return round(seconds / QBLOX_TIMEGRID_INTERVAL) * QBLOX_TIMEGRID_INTERVAL
