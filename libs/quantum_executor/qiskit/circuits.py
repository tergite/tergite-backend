# This code is part of Tergite
#
# (C) Copyright Axel Andersson 2022
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
# The pulses follow the schedules that the tergite SDK's device_compiler built, until it
# sent circuits instead, in tergite/services/device_compiler/schedules.py.
"""The qiskit-pulse schedules of gate-level circuits, for the qiskit-dynamics simulators"""
import math
from typing import Dict, List, Optional, Sequence, Tuple

from qiskit import pulse

from libs.device_parameters import BackendConfig, DeviceCalibration
from libs.device_parameters.dtos import (
    CalibrationValue,
    CouplerCalibration,
    QubitCalibration,
)
from utils.exc import UnsupportedCircuitError

from ..base import gates
from .instruction import WacqtCZPlay

# a coupler's anharmonicity, in Hz, where its calibration has none
_DEFAULT_COUPLER_ANHARMONICITY = -0.17e9
# the wait before and after a CZ, and its rise, in seconds, where the calibration has none
_DEFAULT_CZ_WAIT = 4e-9
_DEFAULT_CZ_RISE = 4e-9


def get_circuit_schedule(
    circuit: gates.NativeCircuit, backend_config: BackendConfig
) -> pulse.Schedule:
    """Gets the pulse schedule of the circuit, from the simulated device's calibration

    An Rxy is a DRAG pulse on the qubit's drive channel, its amplitude scaled from the pi
    pulse's by the angle and its axis turned by phase shifts; an Rz is a phase shift; a CZ
    is the WACQT CZ pulse on the coupler's control channel, followed by each qubit's phase
    correction; and a measurement an acquisition into the memory slot of its channel. Each
    operation starts once its qubits are free. The simulator measures at one time, so every
    measurement is made at the end of the circuit.

    Args:
        circuit: the circuit of native gates
        backend_config: the simulator's config, with its calibration

    Returns:
        the pulse schedule of the circuit

    Raises:
        UnsupportedCircuitError: circuit {name} has {n} qubits, but the device has {m}
        UnsupportedCircuitError: the simulator measures each qubit once, at the end of the
            circuit
        UnsupportedCircuitError: the simulator resets a qubit only before it operates on it
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
        UnsupportedCircuitError: pulse-level blocks run on the quantify executor ...
    """
    calibration = DeviceCalibration.from_config(backend_config)
    qubits = _get_qubit_calibrations(calibration, backend_config)
    if circuit.num_qubits > len(qubits):
        raise UnsupportedCircuitError(
            f"circuit {circuit.name} has {circuit.num_qubits} qubits, "
            f"but the device has {len(qubits)}"
        )

    schedule = pulse.Schedule(name=circuit.name)
    free_at = [0] * circuit.num_qubits
    measures: List[gates.Measure] = []
    for gate in circuit.gates:
        if isinstance(gate, gates.PulseBlock):
            raise UnsupportedCircuitError(
                "pulse-level blocks, cal and defcal, run on the quantify executor, not on "
                "the simulators"
            )
        if isinstance(gate, gates.Measure):
            measures.append(gate)
            continue
        if measures and not isinstance(gate, gates.Barrier):
            raise UnsupportedCircuitError(
                "the simulator measures each qubit once, at the end of the circuit"
            )
        _add_gate(schedule, gate, free_at, qubits, calibration, backend_config)

    _add_acquisitions(schedule, measures, start=max(free_at, default=0))
    return schedule


def _get_qubit_calibrations(
    calibration: DeviceCalibration, backend_config: BackendConfig
) -> List[QubitCalibration]:
    """Gets the calibration of each of the device's qubits, by its index in a circuit"""
    qubits = {qubit.id: qubit for qubit in calibration.qubits}
    return [qubits[number] for number in backend_config.device_config.qubit_numbers]


def _add_gate(
    schedule: pulse.Schedule,
    gate: gates.Gate,
    free_at: List[int],
    qubits: Sequence[QubitCalibration],
    calibration: DeviceCalibration,
    backend_config: BackendConfig,
) -> None:
    """Adds the pulses of a native gate but a measurement where its qubits are free, and
    moves on when they are free again

    Raises:
        UnsupportedCircuitError: the simulator resets a qubit only before it operates on it
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
    """
    dt = backend_config.general_config.dt
    if isinstance(gate, gates.Rxy):
        start = free_at[gate.qubit]
        free_at[gate.qubit] = start + _add_rxy(
            schedule, gate, qubits[gate.qubit], start, dt
        )
    elif isinstance(gate, gates.Rz):
        channel = pulse.DriveChannel(gate.qubit)
        schedule.insert(
            free_at[gate.qubit], pulse.ShiftPhase(gate.theta, channel), inplace=True
        )
    elif isinstance(gate, gates.CZ):
        start = max(free_at[gate.control], free_at[gate.target])
        couplers = _get_couplers(calibration, backend_config)
        end = start + _add_cz(schedule, gate, qubits, couplers, start, dt)
        free_at[gate.control] = free_at[gate.target] = end
    elif isinstance(gate, gates.Reset):
        if free_at[gate.qubit] > 0 or _is_phase_shifted(schedule, gate.qubit):
            raise UnsupportedCircuitError(
                "the simulator resets a qubit only before it operates on it"
            )
    elif isinstance(gate, gates.Delay):
        for qubit in gate.qubits:
            free_at[qubit] += round(gate.duration / dt)
    elif isinstance(gate, gates.Barrier):
        end = max(free_at[qubit] for qubit in gate.qubits)
        for qubit in gate.qubits:
            free_at[qubit] = end


def _add_acquisitions(
    schedule: pulse.Schedule, measures: Sequence[gates.Measure], start: int
) -> None:
    """Adds an acquisition of each measured qubit into its channel's memory slot, all at
    the start

    Raises:
        UnsupportedCircuitError: the simulator measures each qubit once, at the end of the
            circuit
    """
    measured_qubits = [measure.qubit for measure in measures]
    if len(set(measured_qubits)) != len(measured_qubits):
        raise UnsupportedCircuitError(
            "the simulator measures each qubit once, at the end of the circuit"
        )
    for measure in measures:
        acquisition = pulse.Acquire(
            duration=1,
            channel=pulse.AcquireChannel(measure.qubit),
            mem_slot=pulse.MemorySlot(measure.channel),
        )
        schedule.insert(start, acquisition, inplace=True)


def _add_rxy(
    schedule: pulse.Schedule,
    gate: gates.Rxy,
    qubit: QubitCalibration,
    start: int,
    dt: float,
) -> int:
    """Adds the DRAG pulse of the rotation, its axis turned by phase shifts around it

    Returns:
        the pulse's duration in samples
    """
    channel = pulse.DriveChannel(gate.qubit)
    duration = round(qubit.pi_pulse_duration.value / dt)
    sigma = _get_value(qubit.pulse_sigma)
    drag = pulse.Drag(
        duration=duration,
        amp=abs(gate.theta) / math.pi * qubit.pi_pulse_amplitude.value,
        sigma=round(sigma / dt) if sigma else round(duration / 4),
        beta=_get_value(qubit.pi_pulse_motzoi),
        angle=math.pi if gate.theta < 0 else 0.0,
    )
    if gate.phi:
        schedule.insert(start, pulse.ShiftPhase(-gate.phi, channel), inplace=True)
    schedule.insert(start, pulse.Play(drag, channel), inplace=True)
    if gate.phi:
        schedule.insert(
            start + duration, pulse.ShiftPhase(gate.phi, channel), inplace=True
        )
    return duration


def _add_cz(
    schedule: pulse.Schedule,
    gate: gates.CZ,
    qubits: Sequence[QubitCalibration],
    couplers: Dict[Tuple[str, str], Tuple[int, CouplerCalibration]],
    start: int,
    dt: float,
) -> int:
    """Adds the WACQT CZ pulse of the gate's coupler, with its waits, and then each qubit's
    phase correction, the even qubit's as the control's

    Returns:
        the duration of the CZ in samples

    Raises:
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
    """
    control, target = gate.control, gate.target
    if qubits[control].id % 2 != 0:
        control, target = target, control
    coupler_index, coupler = _get_coupler(couplers, qubits[control], qubits[target])

    frequencies = (qubits[control].frequency.value, qubits[target].frequency.value)
    lower, higher = min(frequencies), max(frequencies)
    anharmonicity = _get_value(coupler.anharmonicity, _DEFAULT_COUPLER_ANHARMONICITY)
    t_w = _get_value(coupler.cz_pulse_duration_before, _DEFAULT_CZ_WAIT)
    t_rf = _get_value(coupler.cz_pulse_duration_rise, _DEFAULT_CZ_RISE)
    t_p = _get_value(coupler.cz_pulse_duration_constant)
    duration = round((t_p + t_rf + 2 * t_w) / dt)
    play = WacqtCZPlay(
        channel=pulse.ControlChannel(coupler_index),
        omega_c0=2 * math.pi * _get_value(coupler.frequency),
        theta=_get_value(coupler.cz_pulse_dc_bias),
        omega_phi=_get_value(coupler.frequency_detuning)
        + 2 * math.pi * (lower - higher - anharmonicity),
        phi=_get_value(coupler.cz_pulse_phase_offset),
        t_w=t_w,
        t_rf=t_rf,
        t_p=t_p,
        delta_0=_get_value(coupler.cz_pulse_amplitude),
        duration=duration,
    )
    schedule.insert(start, play, inplace=True)
    for qubit, phase in (
        (control, coupler.control_rz_lambda),
        (target, coupler.target_rz_lambda),
    ):
        shift = pulse.ShiftPhase(_get_value(phase), pulse.DriveChannel(qubit))
        schedule.insert(start + duration, shift, inplace=True)
    return duration


def _is_phase_shifted(schedule: pulse.Schedule, qubit: int) -> bool:
    """Whether the schedule shifts the phase of the qubit's drive"""
    channel = pulse.DriveChannel(qubit)
    return any(
        isinstance(instruction, pulse.ShiftPhase) and instruction.channel == channel
        for _, instruction in schedule.instructions
    )


def _get_couplers(
    calibration: DeviceCalibration, backend_config: BackendConfig
) -> Dict[Tuple[str, str], Tuple[int, CouplerCalibration]]:
    """Gets the index and calibration of each calibrated coupler, by its qubits' names
    in either order"""
    calibrated = {coupler.id: coupler for coupler in calibration.couplers or []}
    couplers = {}
    for name, (first, second) in backend_config.device_config.coupling_dict.items():
        index = int(name.lstrip("u"))
        if index in calibrated:
            couplers[(first, second)] = couplers[(second, first)] = (
                index,
                calibrated[index],
            )
    return couplers


def _get_coupler(
    couplers: Dict[Tuple[str, str], Tuple[int, CouplerCalibration]],
    control: QubitCalibration,
    target: QubitCalibration,
) -> Tuple[int, CouplerCalibration]:
    """Gets the index and calibration of the coupler between the two qubits

    Raises:
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
    """
    try:
        return couplers[(control.name, target.name)]
    except KeyError:
        raise UnsupportedCircuitError(
            f"qubits {control.name} and {target.name} have no coupler"
        )


def _get_value(value: Optional[CalibrationValue], default: float = 0.0) -> float:
    """Gets the calibration value's number, or the default if there is none"""
    if value is None or value.value is None:
        return default
    return float(value.value)
