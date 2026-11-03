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
"""Plays a schedule compiled for the clusters on a simulated chip, and reads out its
acquisitions as the clusters' bins hold them

Each transmon evolves from |0> through the pulses on its drive, each at its clock's
frequency then, detuned from the transmon's true frequency, and is reset to |0> by a reset.
An acquisition reads its transmon's state at its start without collapsing it: an averaging
bin holds the mean of the levels' responses weighted by their populations, an appending
one a response of a level drawn for each shot, each with the noise of its shots. A loop's
body plays once, each of its iterations read out from it, so a body starts with a reset.
"""
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Hashable, List, Optional, Tuple

import numpy as np
from quantify_scheduler.enums import BinMode
from quantify_scheduler.schedules.schedule import ScheduleBase

from libs.device_parameters.dtos import ChipConfig
from utils.exc import UnsupportedCircuitError

from .readout import get_level_responses, get_noise
from .timeline import (
    Acquisition,
    Clocks,
    Pulse,
    Reset,
    Timeline,
    get_timeline,
    get_turns,
)
from .transmon import Transmon

MEASURED_LAST = (
    "the simulated chip reads a transmon once between its resets, after what else "
    "plays on it"
)
_DRIVE_PORT = ":mw"
_READOUT_PORT = ":res"
_PROTOCOLS = ("SSBIntegrationComplex",)


@dataclass(frozen=True)
class AcquiredChannel:
    """What an acquisition channel's bins hold: for an averaging bin mode, the mean of
    each acquisition index's shots, in the order of the indices; for an appending one,
    each shot, those of a repetition in the order they ran, repetition after repetition
    """

    bin_mode: BinMode
    indices: List[int]
    values: np.ndarray


class SimulatedChip:
    """A chip whose schedules, compiled for the clusters, play on its model"""

    def __init__(self, chip: ChipConfig, rng: Optional[np.random.Generator] = None):
        """
        Args:
            chip: the chip's true parameters
            rng: the random generator of its shots' levels and noise; a fresh one if none
        """
        self.chip = chip
        self._rng = rng or np.random.default_rng()
        self._transmons = {name: Transmon(qubit) for name, qubit in chip.qubits.items()}
        self._responses: Dict[Hashable, np.ndarray] = {}

    def play(self, compiled: ScheduleBase) -> Dict[int, AcquiredChannel]:
        """Plays the compiled schedule and reads out its acquisitions

        Args:
            compiled: the schedule, compiled for the clusters

        Returns:
            what each acquisition channel's bins hold, by channel

        Raises:
            ValueError: the simulated chip plays no {operation}
            ValueError: the simulated chip has no transmon {name}
            ValueError: the simulated chip acquires no {protocol}
            ValueError: overlapping pulses drive {name}
            ValueError: acquisition channel {channel} mixes bin modes, or acquires on more
                than one port and clock
            UnsupportedCircuitError: the simulated chip reads a transmon once between its
                resets, after what else plays on it
        """
        timeline = get_timeline(compiled)
        clocks = Clocks(timeline)
        populations: Dict[Acquisition, np.ndarray] = {}
        for name in _get_elements(timeline):
            populations.update(self._evolve(name, timeline, clocks))
        readouts = _get_port_pulses(timeline, _READOUT_PORT)
        responses = {
            acquisition: self._get_responses(acquisition, readouts, clocks)
            for acquisition in timeline.acquisitions
        }
        return self._read_out(timeline, populations, responses)

    def _evolve(
        self, name: str, timeline: Timeline, clocks: Clocks
    ) -> Dict[Acquisition, np.ndarray]:
        """Evolves the transmon from |0> through what plays on it, and gets the
        populations of its levels at each of its acquisitions

        Raises:
            ValueError: the simulated chip has no transmon {name}
            ValueError: overlapping pulses drive {name}
            UnsupportedCircuitError: the simulated chip reads a transmon once between its
                resets, after what else plays on it
        """
        transmon = self._transmons.get(name)
        if transmon is None:
            raise ValueError(f"the simulated chip has no transmon {name}")
        frame = transmon.qubit.frequency
        state, now, is_read = transmon.get_ground_state(), 0, False
        populations = {}
        for start, _, event in _get_events(name, timeline):
            if start > now:
                state, now = transmon.idle(state, start - now), start
            if isinstance(event, Reset):
                state, is_read = transmon.get_ground_state(), False
            elif isinstance(event, Acquisition):
                if is_read:
                    raise UnsupportedCircuitError(MEASURED_LAST)
                populations[event], is_read = transmon.get_populations(state), True
            else:
                if start < now:
                    raise ValueError(f"overlapping pulses drive {name}")
                if is_read:
                    raise UnsupportedCircuitError(MEASURED_LAST)
                frequency = clocks.get_frequency(event.clock, start)
                phase = clocks.get_phases(event.clock, np.array([start]))[0]
                phase = (phase - get_turns(frame, start)) % 1.0
                state = transmon.drive(
                    state, event.samples, event.key, frequency - frame, phase
                )
                now = event.end
        return populations

    def _get_responses(
        self,
        acquisition: Acquisition,
        readouts: Dict[str, List[Pulse]],
        clocks: Clocks,
    ) -> np.ndarray:
        """Gets the acquisition's response to each level of its transmon, kept for an
        acquisition of the same pulses on its own clock

        Raises:
            ValueError: the simulated chip has no transmon {name}
            ValueError: the simulated chip acquires no {protocol}
        """
        if acquisition.protocol not in _PROTOCOLS:
            raise ValueError(f"the simulated chip acquires no {acquisition.protocol}")
        resonator = self.chip.resonators.get(acquisition.element)
        if resonator is None:
            raise ValueError(
                f"the simulated chip has no transmon {acquisition.element}"
            )
        pulses = _get_overlapping(readouts.get(acquisition.port, []), acquisition)
        key = None
        if all(pulse.clock == acquisition.clock for pulse in pulses):
            frequency = clocks.get_frequency(acquisition.clock, acquisition.start)
            key = (acquisition.element, acquisition.duration, frequency)
            key += tuple(
                (pulse.key, pulse.start - acquisition.start) for pulse in pulses
            )
            if key in self._responses:
                return self._responses[key]
        responses = get_level_responses(acquisition, pulses, clocks, resonator)
        if key is not None:
            self._responses[key] = responses
        return responses

    def _read_out(
        self,
        timeline: Timeline,
        populations: Dict[Acquisition, np.ndarray],
        responses: Dict[Acquisition, np.ndarray],
    ) -> Dict[int, AcquiredChannel]:
        """Reads each channel's acquisitions out into its bins"""
        channels: Dict[int, List[Acquisition]] = defaultdict(list)
        for acquisition in timeline.acquisitions:
            channels[acquisition.channel].append(acquisition)
        acquired = {}
        for channel, acquisitions in channels.items():
            bin_modes = {acquisition.bin_mode for acquisition in acquisitions}
            if len(bin_modes) > 1:
                raise ValueError(f"acquisition channel {channel} mixes bin modes")
            if len({(item.port, item.clock) for item in acquisitions}) > 1:
                raise ValueError(
                    f"acquisition channel {channel} acquires on more than one port "
                    "and clock, which the clusters' sequencers cannot tell apart"
                )
            if bin_modes == {BinMode.AVERAGE}:
                acquired[channel] = self._average(
                    acquisitions, timeline.repetitions, populations, responses
                )
            else:
                acquired[channel] = self._append(
                    acquisitions, timeline.repetitions, populations, responses
                )
        return acquired

    def _average(
        self,
        acquisitions: List[Acquisition],
        repetitions: int,
        populations: Dict[Acquisition, np.ndarray],
        responses: Dict[Acquisition, np.ndarray],
    ) -> AcquiredChannel:
        """Averages each acquisition index's shots, each acquisition's mean response with
        the noise of its shots' mean"""
        sums: Dict[int, complex] = defaultdict(complex)
        variances: Dict[int, float] = defaultdict(float)
        counts: Dict[int, int] = defaultdict(int)
        for acquisition in acquisitions:
            shots = repetitions * acquisition.iterations
            sums[acquisition.index] += shots * (
                populations[acquisition] @ responses[acquisition]
            )
            variances[acquisition.index] += shots * self._get_noise(acquisition) ** 2
            counts[acquisition.index] += shots
        indices = sorted(sums)
        values = np.array([sums[index] / counts[index] for index in indices])
        deviations = np.array(
            [np.sqrt(variances[index]) / counts[index] for index in indices]
        )
        values += deviations * (self._rng.normal(size=(len(indices), 2)) @ [1, 1j])
        return AcquiredChannel(BinMode.AVERAGE, indices, values)

    def _append(
        self,
        acquisitions: List[Acquisition],
        repetitions: int,
        populations: Dict[Acquisition, np.ndarray],
        responses: Dict[Acquisition, np.ndarray],
    ) -> AcquiredChannel:
        """Draws each shot of each acquisition, a level and its response with noise, in
        the order they run"""
        starts, indices, shots = [], [], []
        for acquisition in acquisitions:
            iterations = acquisition.iterations
            levels = self._rng.choice(
                len(populations[acquisition]),
                size=(repetitions, iterations),
                p=populations[acquisition],
            )
            noise = self._rng.normal(size=(repetitions, iterations, 2)) @ [1, 1j]
            shots.append(
                responses[acquisition][levels] + self._get_noise(acquisition) * noise
            )
            starts.append(acquisition.get_starts())
            indices.append(np.full(iterations, acquisition.index))
        order = np.argsort(np.concatenate(starts), kind="stable")
        values = np.concatenate(shots, axis=1)[:, order].ravel()
        return AcquiredChannel(
            BinMode.APPEND, np.concatenate(indices)[order].tolist(), values
        )

    def _get_noise(self, acquisition: Acquisition) -> float:
        """Gets the standard deviation of a shot's I, and of its Q, in the acquisition"""
        return get_noise(self.chip.readout_noise, acquisition)


def _get_elements(timeline: Timeline) -> List[str]:
    """Gets the transmons the timeline drives or reads out"""
    elements = {acquisition.element for acquisition in timeline.acquisitions}
    for pulse in timeline.pulses:
        if pulse.port.endswith(_DRIVE_PORT):
            elements.add(pulse.port.split(":")[0])
        elif not pulse.port.endswith(_READOUT_PORT):
            raise ValueError(f"the simulated chip plays no pulse on {pulse.port}")
    return sorted(elements)


def _get_events(name: str, timeline: Timeline) -> List[Tuple[int, int, object]]:
    """Gets the resets of the transmon, its acquisitions and the pulses on its drive, in
    the order they start, a reset before an acquisition before a pulse at the same
    sample"""
    events: List[Tuple[int, int, object]] = [
        (reset.start, 0, reset) for reset in timeline.resets if name in reset.elements
    ]
    events += [
        (acquisition.start, 1, acquisition)
        for acquisition in timeline.acquisitions
        if acquisition.element == name
    ]
    events += [
        (pulse.start, 2, pulse)
        for pulse in timeline.pulses
        if pulse.port == f"{name}{_DRIVE_PORT}"
    ]
    events.sort(key=lambda event: event[:2])
    return events


def _get_port_pulses(timeline: Timeline, suffix: str) -> Dict[str, List[Pulse]]:
    """Gets the pulses on each port of the suffix, in the order they start"""
    pulses: Dict[str, List[Pulse]] = defaultdict(list)
    for pulse in timeline.pulses:
        if pulse.port.endswith(suffix):
            pulses[pulse.port].append(pulse)
    return pulses


def _get_overlapping(pulses: List[Pulse], acquisition: Acquisition) -> List[Pulse]:
    """Gets the pulses, in the order they start, that overlap the acquisition's window"""
    end = acquisition.start + acquisition.duration
    position = bisect_left([pulse.start for pulse in pulses], end)
    return [pulse for pulse in pulses[:position] if pulse.end > acquisition.start]
