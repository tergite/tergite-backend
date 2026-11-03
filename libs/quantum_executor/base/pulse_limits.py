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
"""Checks that a job's pulse-level blocks keep to the limits the device's operators set, as
the device is open to whoever MSS lets send to it"""
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from libs.device_parameters import BackendConfig, DeviceCalibration
from libs.device_parameters.dtos import PulseLimits
from utils.exc import UnsupportedCircuitError

from . import gates


def check_pulse_limits(
    circuits: Sequence[gates.NativeCircuit],
    shots: int,
    limits: Optional[PulseLimits],
    frame_frequencies: Mapping[str, float],
    dt: float,
) -> None:
    """Checks the pulse-level blocks of a job's circuits keep to the device's limits; a job
    of none is not checked

    Each play is checked for its kind of frame, its amplitude and its samples, each change
    of a frame's frequency for how far it moves the frame from its calibrated frequency,
    and each capture for its duration; each frame's energy per shot and over the job, and
    the duration of a circuit's blocks per shot, for theirs.

    Args:
        circuits: the job's circuits of native gates
        shots: the shots of each circuit
        limits: the device's limits; none takes no pulse-level jobs
        frame_frequencies: the calibrated frequency of each of the device's frames, by name
        dt: the device's sample time in seconds

    Raises:
        UnsupportedCircuitError: the device takes no pulse-level jobs: its operators have
            set no pulse limits
        UnsupportedCircuitError: {what}, {value}, is beyond the device's limit of {limit}
        UnsupportedCircuitError: frame {name} is not one of the device's
        UnsupportedCircuitError: {kind} frames may not be played on this device
    """
    blocks = [
        [gate for gate in circuit.gates if isinstance(gate, gates.PulseBlock)]
        for circuit in circuits
    ]
    if not any(blocks):
        return
    if limits is None:
        raise UnsupportedCircuitError(
            "the device takes no pulse-level jobs: its operators have set no pulse limits"
        )

    job_energies: Dict[gates.Frame, float] = {}
    for circuit_blocks in blocks:
        shot_energies = _check_circuit_blocks(
            circuit_blocks, limits, frame_frequencies, dt
        )
        for frame, energy in shot_energies.items():
            job_energies[frame] = job_energies.get(frame, 0.0) + energy * shots
    for frame, energy in job_energies.items():
        limit = _get_kind_limit(limits.max_energy_per_job, frame)
        _check_at_most(f"the energy on {frame.name} over the job", energy, limit)


def get_calibrated_frame_frequencies(backend_config: BackendConfig) -> Dict[str, float]:
    """Gets the calibrated frequency of each frame the device publishes, from its
    calibration: each qubit's drive and readout, and each coupler's flux at its CZ's

    Args:
        backend_config: the device's backend config, with its calibration

    Returns:
        the frequencies in hertz, by the frames' names, e.g. q12_drive; a frame of no
        calibrated frequency, as of a device not calibrated, is left out
    """
    if backend_config.calibration_config is None:
        return {}
    calibration = DeviceCalibration.from_config(backend_config)
    frequencies = {}
    for qubit in calibration.qubits:
        if qubit.frequency is not None:
            frequencies[f"q{qubit.id:02d}_drive"] = float(qubit.frequency.value)
    for resonator in calibration.resonators or []:
        if resonator.frequency is not None:
            frequencies[f"q{resonator.id:02d}_readout"] = float(
                resonator.frequency.value
            )
    for coupler in calibration.couplers or []:
        if coupler.frequency is not None and coupler.full_name:
            qubits = (int(q.lstrip("q")) for q in coupler.full_name.split("_"))
            name = "_".join(f"q{qubit_id:02d}" for qubit_id in qubits)
            frequencies[f"{name}_coupler"] = float(coupler.frequency.value)
    return frequencies


def _check_circuit_blocks(
    blocks: List[gates.PulseBlock],
    limits: PulseLimits,
    frame_frequencies: Mapping[str, float],
    dt: float,
) -> Dict[gates.Frame, float]:
    """Checks a circuit's blocks keep to the limits, and gets each frame's energy in a shot

    Raises:
        UnsupportedCircuitError: {what}, {value}, is beyond the device's limit of {limit}
        UnsupportedCircuitError: frame {name} is not one of the device's
        UnsupportedCircuitError: {kind} frames may not be played on this device
    """
    energies: Dict[gates.Frame, float] = {}
    frequencies: Dict[gates.Frame, float] = {}
    for block in blocks:
        for op in block.operations:
            if isinstance(op, gates.FrameBarrier):
                continue
            calibrated = _get_calibrated_frequency(op.frame, frame_frequencies)
            if isinstance(op, gates.Play):
                energies[op.frame] = energies.get(op.frame, 0.0) + _check_play(
                    op, limits, dt
                )
            elif isinstance(op, (gates.ShiftFrequency, gates.SetFrequency)):
                current = frequencies.get(op.frame, calibrated)
                if isinstance(op, gates.ShiftFrequency):
                    frequencies[op.frame] = current + op.frequency
                else:
                    frequencies[op.frame] = op.frequency
                offset = abs(frequencies[op.frame] - calibrated)
                _check_at_most(
                    f"moving {op.frame.name} from its calibrated frequency",
                    offset,
                    limits.max_frequency_offset,
                    unit=(1e6, "MHz"),
                )
            elif isinstance(op, gates.Capture):
                _check_at_most(
                    f"a capture on {op.frame.name}",
                    op.duration,
                    limits.max_capture_duration,
                    unit=(1e-6, "us"),
                )

    duration = sum(_get_block_duration(block, dt) for block in blocks)
    _check_at_most(
        "the pulse-level blocks of a shot",
        duration,
        limits.max_pulse_duration,
        unit=(1e-6, "us"),
    )
    for frame, energy in energies.items():
        limit = _get_kind_limit(limits.max_energy_per_shot, frame)
        _check_at_most(f"the energy on {frame.name} in a shot", energy, limit)
    return energies


def _check_play(play: gates.Play, limits: PulseLimits, dt: float) -> float:
    """Checks a play keeps to the limits of its frame's kind, and gets its energy

    Raises:
        UnsupportedCircuitError: {what}, {value}, is beyond the device's limit of {limit}
        UnsupportedCircuitError: {kind} frames may not be played on this device
    """
    samples = np.abs(np.asarray(play.samples))
    _check_at_most(
        f"the amplitude of a waveform on {play.frame.name}",
        float(np.max(samples)),
        _get_kind_limit(limits.max_amplitude, play.frame),
    )
    _check_at_most(
        f"the samples of a waveform on {play.frame.name}",
        len(samples),
        limits.max_samples_per_waveform,
    )
    return float(np.sum(samples**2) * dt)


def _get_calibrated_frequency(
    frame: gates.Frame, frame_frequencies: Mapping[str, float]
) -> float:
    """Gets the frame's calibrated frequency

    Raises:
        UnsupportedCircuitError: frame {name} is not one of the device's
    """
    if frame.name not in frame_frequencies:
        raise UnsupportedCircuitError(f"frame {frame.name} is not one of the device's")
    return frame_frequencies[frame.name]


def _get_kind_limit(limits_by_kind: Mapping[str, float], frame: gates.Frame) -> float:
    """Gets the limit of the frame's kind

    Raises:
        UnsupportedCircuitError: {kind} frames may not be played on this device
    """
    if frame.kind not in limits_by_kind:
        raise UnsupportedCircuitError(
            f"{frame.kind} frames may not be played on this device"
        )
    return limits_by_kind[frame.kind]


def _check_at_most(
    what: str, value: float, limit: float, unit: Tuple[float, str] = (1.0, "")
) -> None:
    """Checks the value of what is described is at most the limit, both shown in the unit
    of the given size and name

    Raises:
        UnsupportedCircuitError: {what}, {value}, is beyond the device's limit of {limit}
    """
    if value > limit * (1 + 1e-9):
        size, name = unit
        raise UnsupportedCircuitError(
            f"{what}, {value / size:.6g} {name}, is beyond the device's limit of "
            f"{limit / size:.6g} {name}".replace(" ,", ",").rstrip()
        )


def _get_block_duration(block: gates.PulseBlock, dt: float) -> float:
    """The seconds a block takes: its longest frame, each running its plays, delays and
    captures one after the other, the barriers aligning their frames"""
    cursors: Dict[gates.Frame, float] = {}
    for op in block.operations:
        if isinstance(op, gates.FrameBarrier):
            aligned = op.frames or tuple(cursors)
            end = max((cursors.get(frame, 0.0) for frame in aligned), default=0.0)
            cursors.update({frame: end for frame in aligned})
        elif isinstance(op, gates.Play):
            cursors[op.frame] = cursors.get(op.frame, 0.0) + len(op.samples) * dt
        elif isinstance(op, (gates.FrameDelay, gates.Capture)):
            cursors[op.frame] = cursors.get(op.frame, 0.0) + op.duration
    return max(cursors.values(), default=0.0)
