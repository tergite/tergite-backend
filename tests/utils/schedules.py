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
"""What each qubit gets of a compiled schedule, and how two schedules differ in it"""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
from quantify_scheduler.enums import BinMode

from libs.quantum_executor.quantify.simulated_chip.timeline import (
    Acquisition,
    ClockChange,
    Clocks,
    Pulse,
    Timeline,
    get_turns,
)
from tests.utils.snapshots import to_samples

# how far apart samples, frequencies, in hertz, and phases, in turns, may be and be equal
_SAMPLE_TOLERANCE = 1e-9
_FREQUENCY_TOLERANCE = 1e-3
_PHASE_TOLERANCE = 1e-6


@dataclass(frozen=True)
class QubitEvent:
    """A pulse or acquisition a qubit gets, with what decides what it does to the qubit

    Attributes:
        kind: "pulse" or "acquisition"
        start: its first sample
        port: the port it plays or acquires on
        frequency: its clock's frequency at its start, in hertz
        samples: a pulse's envelope; empty for an acquisition
        frame_phase: the phase of a drive or flux pulse's clock at its start, in turns,
            against a frame turning at the clock's frequency from the first sample: the
            axis it turns the qubit about. None for readout, whose acquisition shares the
            pulse's clock, so that only the time between them turns what it reads
        duration: the samples it lasts
        protocol: an acquisition's protocol
        bin_mode: an acquisition's bin mode
    """

    kind: str
    start: int
    port: str
    frequency: float
    samples: np.ndarray = field(default_factory=lambda: np.zeros(0, complex))
    frame_phase: Optional[float] = None
    duration: int = 0
    protocol: str = ""
    bin_mode: str = ""


def to_timeline(snapshot: Dict[str, Any], waveforms: Dict[str, Any]) -> Timeline:
    """Gets the timeline of a snapshot

    Args:
        snapshot: the snapshot, as to_snapshot gives it
        waveforms: the waveforms of its pulses, by their names

    Returns:
        the timeline, without its resets
    """
    return Timeline(
        repetitions=snapshot["repetitions"],
        frequencies=snapshot["frequencies"],
        pulses=[
            Pulse(
                start=pulse["start"],
                port=pulse["port"],
                clock=pulse["clock"],
                samples=to_samples(waveforms[pulse["waveform"]]),
                key=pulse["waveform"],
            )
            for pulse in snapshot["pulses"]
        ],
        clock_changes=[ClockChange(**change) for change in snapshot["clock_changes"]],
        acquisitions=[
            Acquisition(
                **acquisition
                | {
                    "bin_mode": BinMode(acquisition["bin_mode"]),
                    "loops": tuple(tuple(loop) for loop in acquisition["loops"]),
                }
            )
            for acquisition in snapshot["acquisitions"]
        ],
    )


def get_qubit_events(timeline: Timeline) -> Dict[str, List[QubitEvent]]:
    """Gets the pulses and acquisitions each qubit gets, in the order they start: a
    coupler's flux pulses go to both its qubits. A reset plays nothing, so is none

    Args:
        timeline: the timeline of a compiled schedule

    Returns:
        the events of each qubit, by its name
    """
    clocks = Clocks(timeline)
    events = [_get_pulse_event(pulse, timeline, clocks) for pulse in timeline.pulses]
    events += [
        QubitEvent(
            kind="acquisition",
            start=acquisition.start,
            port=acquisition.port,
            frequency=clocks.get_frequency(acquisition.clock, acquisition.start),
            duration=acquisition.duration,
            protocol=acquisition.protocol,
            bin_mode=str(acquisition.bin_mode.value),
        )
        for acquisition in timeline.acquisitions
    ]
    qubit_events: Dict[str, List[QubitEvent]] = {}
    for event in sorted(events, key=lambda item: (item.start, item.kind, item.port)):
        for qubit in _get_qubits(event.port):
            qubit_events.setdefault(qubit, []).append(event)
    return qubit_events


def get_differences(expected: Timeline, got: Timeline) -> List[str]:
    """Gets how what each qubit gets of one timeline differs from what it gets of the
    other: which pulses and acquisitions, in which order, and what each does to it,
    whatever their starts

    Args:
        expected: the timeline expected
        got: the timeline got

    Returns:
        a line for each difference, none if they do not differ
    """
    expected_events = get_qubit_events(expected)
    got_events = get_qubit_events(got)
    differences = []
    for qubit in sorted(set(expected_events) | set(got_events)):
        wanted = expected_events.get(qubit, [])
        played = got_events.get(qubit, [])
        if [_describe(event) for event in wanted] != [
            _describe(event) for event in played
        ]:
            differences.append(
                f"{qubit} gets {[_describe(event) for event in played]}, "
                f"not {[_describe(event) for event in wanted]}"
            )
            continue
        for idx, (want, play) in enumerate(zip(wanted, played)):
            differences += [
                f"{qubit}'s {_describe(want)} #{idx}: {difference}"
                for difference in _get_event_differences(want, play)
            ]
    return differences


def get_idle_time(events: List[QubitEvent]) -> int:
    """Gets the samples a qubit idles between the start of its first drive or flux pulse
    and the start of its last readout pulse, in which it decoheres

    Args:
        events: the qubit's events, in the order they start

    Returns:
        the samples, 0 if it gets no drive or flux pulse
    """
    controls = [
        event
        for event in events
        if event.kind == "pulse" and not event.port.endswith(":res")
    ]
    readouts = [
        event
        for event in events
        if event.kind == "pulse" and event.port.endswith(":res")
    ]
    if not controls or not readouts or readouts[-1].start < controls[0].start:
        return 0
    idle, busy_until = 0, controls[0].start
    for event in events:
        if not controls[0].start <= event.start <= readouts[-1].start:
            continue
        idle += max(event.start - busy_until, 0)
        busy_until = max(busy_until, event.start + event.duration)
    return idle


def get_times_of_flight(events: List[QubitEvent]) -> List[int]:
    """Gets the samples from the start of each of a qubit's acquisitions back to the
    start of its readout pulse before it

    Args:
        events: the qubit's events, in the order they start

    Returns:
        the samples of each acquisition, in order
    """
    times, readout_start = [], None
    for event in events:
        if event.kind == "pulse" and event.port.endswith(":res"):
            readout_start = event.start
        elif event.kind == "acquisition" and readout_start is not None:
            times.append(event.start - readout_start)
    return times


def _get_pulse_event(pulse: Pulse, timeline: Timeline, clocks: Clocks) -> QubitEvent:
    """Gets the event of a pulse, with its clock's frame phase unless it reads out"""
    frame_phase = None
    if not pulse.port.endswith(":res"):
        phase = float(clocks.get_phases(pulse.clock, np.array([pulse.start]))[0])
        frame_turns = get_turns(timeline.frequencies[pulse.clock], pulse.start)
        frame_phase = (phase - frame_turns) % 1.0
    return QubitEvent(
        kind="pulse",
        start=pulse.start,
        port=pulse.port,
        frequency=clocks.get_frequency(pulse.clock, pulse.start),
        samples=pulse.samples,
        frame_phase=frame_phase,
        duration=len(pulse.samples),
    )


def _get_qubits(port: str) -> List[str]:
    """Gets the qubits a port plays to: a coupler's both"""
    return port.split(":")[0].split("_")


def _describe(event: QubitEvent) -> str:
    """Gets a short name of the event: its kind and port"""
    return f"{event.kind} on {event.port}"


def _get_event_differences(expected: QubitEvent, got: QubitEvent) -> List[str]:
    """Gets how an event differs from the one expected, as lines"""
    differences = []
    if expected.samples.shape != got.samples.shape or not np.allclose(
        expected.samples, got.samples, atol=_SAMPLE_TOLERANCE, rtol=0
    ):
        differences.append(
            f"{len(got.samples)} samples up to {_get_peak(got.samples):.6g}, "
            f"not {len(expected.samples)} up to {_get_peak(expected.samples):.6g}"
        )
    if not math.isclose(
        expected.frequency, got.frequency, abs_tol=_FREQUENCY_TOLERANCE
    ):
        differences.append(f"frequency {got.frequency} Hz, not {expected.frequency} Hz")
    if expected.frame_phase is not None and got.frame_phase is not None:
        distance = (got.frame_phase - expected.frame_phase) % 1.0
        if min(distance, 1.0 - distance) > _PHASE_TOLERANCE:
            differences.append(
                f"frame phase {got.frame_phase:.6f} turns, "
                f"not {expected.frame_phase:.6f}"
            )
    for name in ("duration", "protocol", "bin_mode"):
        if getattr(expected, name) != getattr(got, name):
            differences.append(
                f"{name} {getattr(got, name)}, not {getattr(expected, name)}"
            )
    return differences


def _get_peak(samples: np.ndarray) -> float:
    """Gets the largest magnitude of the samples, 0 if none"""
    return float(np.abs(samples).max()) if len(samples) else 0.0
