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
"""What a schedule compiled for the clusters plays, on the clusters' grid of samples: its
pulses, the changes of its clocks, its resets and its acquisitions, a loop's body once
"""
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Iterator, List, Optional, Tuple

import numpy as np
from quantify_scheduler.enums import BinMode
from quantify_scheduler.helpers.waveforms import get_waveform
from quantify_scheduler.operations.control_flow_library import LoopOperation
from quantify_scheduler.schedules.schedule import ScheduleBase

# the clusters' sampling interval, in seconds
SAMPLE = 1e-9
# the waveforms sampled, by their pulses' keys, and how many are kept
_WAVEFORMS: Dict[Hashable, np.ndarray] = {}
_MAX_WAVEFORMS = 100_000


@dataclass(frozen=True)
class Pulse:
    """A pulse, its envelope sampled on the clusters' grid, from its first sample"""

    start: int
    port: str
    clock: str
    samples: np.ndarray
    # the same for pulses of the same samples
    key: Hashable

    @property
    def end(self) -> int:
        """The sample after its last"""
        return self.start + len(self.samples)


@dataclass(frozen=True)
class ClockChange:
    """A change of a clock: its phase reset to 0, shifted by turns, or its frequency set,
    to its own again where none"""

    start: int
    clock: str
    is_reset: bool = False
    phase_shift: float = 0.0
    frequency: Optional[float] = None
    is_frequency_set: bool = False


@dataclass(frozen=True)
class Reset:
    """A reset of transmons to |0>"""

    start: int
    elements: Tuple[str, ...]


@dataclass(frozen=True)
class Acquisition:
    """An acquisition, with the loops it is in, outermost first, each its repetitions and
    the samples of one of them"""

    start: int
    duration: int
    port: str
    clock: str
    channel: int
    index: int
    bin_mode: BinMode
    protocol: str
    loops: Tuple[Tuple[int, int], ...] = ()

    @property
    def element(self) -> str:
        """The transmon it reads out"""
        return self.port.split(":")[0]

    @property
    def iterations(self) -> int:
        """How many times it runs in one repetition of the schedule"""
        return math.prod(repetitions for repetitions, _ in self.loops)

    def get_starts(self) -> np.ndarray:
        """Gets its start in each iteration of its loops, in the order they run"""
        starts = np.array([self.start])
        for repetitions, period in self.loops:
            starts = (starts[:, np.newaxis] + period * np.arange(repetitions)).ravel()
        return starts


@dataclass
class Timeline:
    """What a compiled schedule plays, each in the order of its start"""

    repetitions: int
    pulses: List[Pulse] = field(default_factory=list)
    clock_changes: List[ClockChange] = field(default_factory=list)
    resets: List[Reset] = field(default_factory=list)
    acquisitions: List[Acquisition] = field(default_factory=list)
    # each clock's frequency where no change sets it, in hertz
    frequencies: Dict[str, float] = field(default_factory=dict)


class Clocks:
    """The clocks of a timeline, their phases in turns and their frequencies at each sample,
    as the clusters' oscillators keep them: phase continuous across a change of frequency,
    from 0 at the start of each repetition"""

    def __init__(self, timeline: Timeline) -> None:
        """
        Args:
            timeline: the timeline whose clocks these are
        """
        self._starts: Dict[str, List[int]] = {}
        # each change's sample, phase then and frequency after it
        self._states: Dict[str, List[Tuple[int, float, float]]] = {}
        for clock, frequency in timeline.frequencies.items():
            self._starts[clock] = [0]
            self._states[clock] = [(0, 0.0, frequency)]
        for change in sorted(timeline.clock_changes, key=lambda item: item.start):
            if change.clock not in self._states:
                continue
            start, phase, frequency = self._get_state(change.clock, change.start)
            phase = _get_phase(phase, frequency, change.start - start)
            if change.is_reset:
                phase = 0.0
            phase = (phase + change.phase_shift) % 1.0
            if change.is_frequency_set:
                frequency = (
                    timeline.frequencies[change.clock]
                    if change.frequency is None
                    else change.frequency
                )
            self._starts[change.clock].append(change.start)
            self._states[change.clock].append((change.start, phase, frequency))

    def get_frequency(self, clock: str, sample: int) -> float:
        """Gets the clock's frequency at the sample, in hertz

        Args:
            clock: the clock's name
            sample: the sample

        Returns:
            its frequency

        Raises:
            KeyError: the clock has no frequency
        """
        return self._get_state(clock, sample)[2]

    def get_phases(self, clock: str, samples: np.ndarray) -> np.ndarray:
        """Gets the clock's phase at each of the samples, in turns, as of the state at the
        first: none of its changes after the first is applied

        Args:
            clock: the clock's name
            samples: the samples, the first the earliest

        Returns:
            its phase at each, in [0, 1)

        Raises:
            KeyError: the clock has no frequency
        """
        start, phase, frequency = self._get_state(clock, int(samples[0]))
        return _get_phase(phase, frequency, np.asarray(samples) - start)

    def _get_state(self, clock: str, sample: int) -> Tuple[int, float, float]:
        """Gets the last change of the clock at or before the sample"""
        position = bisect_right(self._starts[clock], sample) - 1
        return self._states[clock][max(position, 0)]


def get_timeline(compiled: ScheduleBase) -> Timeline:
    """Gets what the compiled schedule plays, a loop's body once, at its first iteration

    Args:
        compiled: the schedule, compiled for the clusters

    Returns:
        its timeline

    Raises:
        ValueError: the simulated chip plays no {operation}
    """
    timeline = Timeline(repetitions=compiled.repetitions)
    offsets: Dict[Tuple[str, str], List[Tuple[int, complex]]] = {}
    for start, operation, loops in _get_timed_operations(compiled, 0, ()):
        _add_operation(timeline, offsets, start, operation, loops)
    timeline.pulses.extend(_get_offset_pulses(offsets))
    timeline.pulses.sort(key=lambda pulse: pulse.start)
    timeline.clock_changes.sort(key=lambda change: change.start)
    timeline.resets.sort(key=lambda reset: reset.start)
    timeline.acquisitions.sort(key=lambda acquisition: acquisition.start)
    timeline.frequencies = _get_clock_frequencies(compiled)
    return timeline


def get_turns(frequency: float, samples: Any) -> Any:
    """Gets the turns of a frequency over the samples, modulo 1

    Args:
        frequency: the frequency, in hertz
        samples: a number of samples, or an array of them

    Returns:
        the turns, in [0, 1)
    """
    return (math.fmod(frequency * SAMPLE, 1.0) * samples) % 1.0


def _get_timed_operations(
    schedule: ScheduleBase, offset: int, loops: Tuple[Tuple[int, int], ...]
) -> Iterator[Tuple[int, Any, Tuple[Tuple[int, int], ...]]]:
    """Gets each operation of the schedule, those in its schedules and loops too, with its
    start in samples and the loops it is in

    Raises:
        ValueError: the simulated chip plays no {operation}
    """
    for schedulable in schedule.schedulables.values():
        start = offset + _to_samples(schedulable["abs_time"])
        operation = schedule.operations[schedulable["operation_id"]]
        if isinstance(operation, ScheduleBase):
            yield from _get_timed_operations(operation, start, loops)
        elif isinstance(operation, LoopOperation):
            body = operation.body
            loop = (operation.data["control_flow_info"]["repetitions"],)
            loop += (_to_samples(body.duration),)
            if isinstance(body, ScheduleBase):
                yield from _get_timed_operations(body, start, loops + (loop,))
            else:
                yield start, body, loops + (loop,)
        elif "control_flow_info" in operation.data:
            raise ValueError(f"the simulated chip plays no {operation.name}")
        else:
            yield start, operation, loops


def _add_operation(
    timeline: Timeline,
    offsets: Dict[Tuple[str, str], List[Tuple[int, complex]]],
    start: int,
    operation: Any,
    loops: Tuple[Tuple[int, int], ...],
) -> None:
    """Adds what the operation plays to the timeline, its voltage offsets to the offsets
    of their port and clock"""
    gate_info = operation.data.get("gate_info", {})
    if gate_info.get("operation_type") == "reset":
        elements = tuple(gate_info["device_elements"])
        timeline.resets.append(Reset(start=start, elements=elements))
        return
    for info in operation.data.get("pulse_info", []):
        _add_pulse_info(timeline, offsets, start + _to_samples(info["t0"]), info)
    for info in operation.data.get("acquisition_info", []):
        timeline.acquisitions.append(
            Acquisition(
                start=start + _to_samples(info["t0"]),
                duration=_to_samples(info["duration"]),
                port=info["port"],
                clock=info["clock"],
                channel=info["acq_channel"],
                index=info["acq_index"],
                bin_mode=BinMode(info["bin_mode"]),
                protocol=info["protocol"],
                loops=loops,
            )
        )


def _add_pulse_info(
    timeline: Timeline,
    offsets: Dict[Tuple[str, str], List[Tuple[int, complex]]],
    start: int,
    info: Dict[str, Any],
) -> None:
    """Adds the pulse, voltage offset or clock change of the pulse info"""
    clock = info.get("clock")
    if "offset_path_I" in info:
        amplitude = complex(info["offset_path_I"], info["offset_path_Q"])
        offsets.setdefault((info["port"], clock), []).append((start, amplitude))
    elif info.get("reset_clock_phase"):
        timeline.clock_changes.append(ClockChange(start, clock, is_reset=True))
    elif "phase_shift" in info:
        # a shift turns the carrier back, so that it turns later pulses' axes the other
        # way from their own phase, as a virtual Z of quantify's gates does
        shift = -info["phase_shift"] / 360
        timeline.clock_changes.append(ClockChange(start, clock, phase_shift=shift))
    elif "clock_freq_new" in info:
        timeline.clock_changes.append(
            ClockChange(
                start, clock, frequency=info["clock_freq_new"], is_frequency_set=True
            )
        )
    elif info.get("wf_func") is not None:
        key = _get_key(info)
        samples = _get_samples(key, info)
        timeline.pulses.append(Pulse(start, info["port"], clock, samples, key))


def _get_offset_pulses(
    offsets: Dict[Tuple[str, str], List[Tuple[int, complex]]],
) -> Iterator[Pulse]:
    """Gets the pulses of the voltage offsets, each held until the next on its port and
    clock"""
    for (port, clock), changes in offsets.items():
        changes = sorted(changes, key=lambda change: change[0])
        for (start, amplitude), (end, _) in zip(changes, changes[1:]):
            if amplitude and end > start:
                samples = np.full(end - start, amplitude)
                yield Pulse(
                    start, port, clock, samples, ("offset", amplitude, end - start)
                )


def _get_clock_frequencies(schedule: ScheduleBase) -> Dict[str, float]:
    """Gets the frequency of each clock of the schedule and of those within it, in hertz"""
    frequencies = {}
    for operation in schedule.operations.values():
        if isinstance(operation, LoopOperation):
            operation = operation.body
        if isinstance(operation, ScheduleBase):
            frequencies.update(_get_clock_frequencies(operation))
    for name, resource in schedule.resources.items():
        frequency = resource.data.get("freq")
        if frequency is not None and not math.isnan(frequency):
            frequencies[name] = frequency
    return frequencies


def _get_samples(key: Hashable, info: Dict[str, Any]) -> np.ndarray:
    """Gets the samples of the pulse info, kept by its key"""
    samples = _WAVEFORMS.get(key)
    if samples is None:
        samples = np.asarray(get_waveform(info, sampling_rate=1 / SAMPLE), complex)
        samples.setflags(write=False)
        if len(_WAVEFORMS) > _MAX_WAVEFORMS:
            _WAVEFORMS.clear()
        _WAVEFORMS[key] = samples
    return samples


def _get_key(info: Dict[str, Any]) -> Hashable:
    """Gets a key of what the pulse info plays, wherever it starts"""
    return tuple(
        (name, _get_hashable(value))
        for name, value in sorted(info.items())
        if name != "t0"
    )


def _get_hashable(value: Any) -> Hashable:
    """Gets a hashable of the value, an array by its bytes"""
    if isinstance(value, np.ndarray):
        return value.dtype.str, value.shape, value.tobytes()
    if isinstance(value, (list, tuple)):
        return tuple(_get_hashable(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (name, _get_hashable(item)) for name, item in sorted(value.items())
        )
    return value


def _to_samples(seconds: float) -> int:
    """Gets the samples of the seconds, to the nearest"""
    return round(seconds / SAMPLE)


def _get_phase(phase: float, frequency: float, samples: Any) -> Any:
    """Gets the phase, in turns, after the samples at the frequency, from the phase"""
    return (phase + get_turns(frequency, samples)) % 1.0
