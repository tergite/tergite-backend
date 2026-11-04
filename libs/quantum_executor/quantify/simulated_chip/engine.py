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
A coupler's flux pulse evolves its two transmons together: the transmons that couplers'
pulses join in a schedule evolve as one register. An acquisition reads its transmon's
state at its start without collapsing it: an averaging bin holds the mean of the levels'
responses weighted by their populations, an appending one a response of a level drawn for
each shot, jointly with the others acquired at once, each with the noise of its shots. A
loop's body plays once, each of its iterations read out from it, so a body starts with a
reset.
"""
import math
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Hashable, List, Optional, Sequence, Tuple, Union

import numpy as np
from quantify_scheduler.enums import BinMode
from quantify_scheduler.schedules.schedule import ScheduleBase

from libs.device_parameters.dtos import ChipConfig
from utils.exc import UnsupportedCircuitError

from .coupler import DOWNCONVERSION_FREQUENCY, PAIR_STATES, Coupler
from .readout import get_level_responses, get_noise
from .register import Register
from .timeline import (
    SAMPLE,
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
_FLUX_PORT = ":fl"
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

    def __init__(
        self,
        chip: ChipConfig,
        rng: Optional[np.random.Generator] = None,
        should_follow_currents: bool = False,
    ) -> None:
        """
        Args:
            chip: the chip's true parameters
            rng: the random generator of its shots' levels and noise; a fresh one if none
            should_follow_currents: whether the DC currents set on its SPI rack move its
                couplers' flux; without a rack, each sits at its flux offset, as at a
                current set and watched by hand
        """
        self.chip = chip
        self.should_follow_currents = should_follow_currents
        self._rng = rng or np.random.default_rng()
        self._currents: Dict[str, float] = {}
        self._couplers = _get_couplers(chip, self._currents)
        self._transmons = _get_transmons(chip, self._couplers)
        self._responses: Dict[Hashable, np.ndarray] = {}

    def set_current(self, coupler: str, current: float) -> None:
        """Sets the DC current of a coupler's SPI rack DAC, which moves its flux, and
        with it its transmons' frequencies, if the chip follows its SPI rack

        Args:
            coupler: the coupler's name
            current: the current, in amperes
        """
        if not self.should_follow_currents:
            return
        self._currents[coupler] = current
        self._couplers = _get_couplers(self.chip, self._currents)
        self._transmons = _get_transmons(self.chip, self._couplers)

    def run(self, compiled: ScheduleBase) -> Dict[int, AcquiredChannel]:
        """Plays the compiled schedule and reads out its acquisitions

        Args:
            compiled: the schedule, compiled for the clusters

        Returns:
            what each acquisition channel's bins hold, by channel

        Raises:
            ValueError: the simulated chip plays no {operation}
            ValueError: the simulated chip has no transmon {name}, or no coupler {name}
            ValueError: the simulated chip acquires no {protocol}
            ValueError: overlapping pulses drive {name}
            ValueError: acquisition channel {channel} mixes bin modes, or acquires on more
                than one port and clock
            UnsupportedCircuitError: the simulated chip reads a transmon once between its
                resets, after what else plays on it
        """
        timeline = get_timeline(compiled)
        clocks = Clocks(timeline)
        readouts: Dict[Acquisition, _Readout] = {}
        is_coupled = self.chip.should_simulate_static_zz
        for elements in _get_groups(timeline, self._couplers, is_coupled):
            group = _Group(
                elements, self._transmons, self._couplers, clocks, is_coupled
            )
            readouts.update(group.run(_get_events(elements, timeline)))
        pulses = _get_port_pulses(timeline, _READOUT_PORT)
        responses = {
            acquisition: self._get_responses(acquisition, pulses, clocks)
            for acquisition in timeline.acquisitions
        }
        return self._read_out(timeline, readouts, responses)

    def _get_responses(
        self,
        acquisition: Acquisition,
        pulses: Dict[str, List[Pulse]],
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
        overlapping = _get_overlapping(pulses.get(acquisition.port, []), acquisition)
        key = None
        if all(pulse.clock == acquisition.clock for pulse in overlapping):
            frequency = clocks.get_frequency(acquisition.clock, acquisition.start)
            key = (acquisition.element, acquisition.duration, frequency)
            key += tuple(
                (pulse.key, pulse.start - acquisition.start) for pulse in overlapping
            )
            if key in self._responses:
                return self._responses[key]
        responses = get_level_responses(acquisition, overlapping, clocks, resonator)
        if key is not None:
            self._responses[key] = responses
        return responses

    def _read_out(
        self,
        timeline: Timeline,
        readouts: Dict[Acquisition, "_Readout"],
        responses: Dict[Acquisition, np.ndarray],
    ) -> Dict[int, AcquiredChannel]:
        """Reads each channel's acquisitions out into its bins

        Raises:
            ValueError: acquisition channel {channel} mixes bin modes, or acquires on more
                than one port and clock
        """
        levels = self._generate_levels(readouts, timeline.repetitions)
        acquired = {}
        for channel, acquisitions in _get_channels(timeline).items():
            if acquisitions[0].bin_mode == BinMode.AVERAGE:
                acquired[channel] = self._get_averages(
                    acquisitions, timeline.repetitions, readouts, responses
                )
            else:
                acquired[channel] = self._get_shots(acquisitions, levels, responses)
        return acquired

    def _generate_levels(
        self, readouts: Dict[Acquisition, "_Readout"], repetitions: int
    ) -> Dict[Acquisition, np.ndarray]:
        """Generates the level of each shot of each appending acquisition, jointly with the
        others of its readout, by repetition and iteration"""
        levels = {}
        for readout in set(readouts.values()):
            first = readout.acquisitions[0]
            if first.bin_mode != BinMode.APPEND:
                continue
            probabilities = readout.probabilities.ravel()
            drawn = self._rng.choice(
                len(probabilities),
                size=(repetitions, first.iterations),
                p=probabilities,
            )
            joint = np.unravel_index(drawn, readout.probabilities.shape)
            levels.update(zip(readout.acquisitions, joint))
        return levels

    def _get_averages(
        self,
        acquisitions: List[Acquisition],
        repetitions: int,
        readouts: Dict[Acquisition, "_Readout"],
        responses: Dict[Acquisition, np.ndarray],
    ) -> AcquiredChannel:
        """Averages each acquisition index's shots, each acquisition's mean response with
        the noise of its shots' mean"""
        sums: Dict[int, complex] = defaultdict(complex)
        variances: Dict[int, float] = defaultdict(float)
        counts: Dict[int, int] = defaultdict(int)
        for acquisition in acquisitions:
            shots = repetitions * acquisition.iterations
            populations = readouts[acquisition].get_populations(acquisition)
            sums[acquisition.index] += shots * (populations @ responses[acquisition])
            variances[acquisition.index] += shots * self._get_noise(acquisition) ** 2
            counts[acquisition.index] += shots
        indices = sorted(sums)
        values = np.array([sums[index] / counts[index] for index in indices])
        deviations = np.array(
            [np.sqrt(variances[index]) / counts[index] for index in indices]
        )
        values += deviations * (self._rng.normal(size=(len(indices), 2)) @ [1, 1j])
        return AcquiredChannel(BinMode.AVERAGE, indices, values)

    def _get_shots(
        self,
        acquisitions: List[Acquisition],
        levels: Dict[Acquisition, np.ndarray],
        responses: Dict[Acquisition, np.ndarray],
    ) -> AcquiredChannel:
        """Gets each shot of each acquisition, its level's response with noise, in the
        order they run"""
        starts, indices, shots = [], [], []
        for acquisition in acquisitions:
            drawn = levels[acquisition]
            noise = self._rng.normal(size=(*drawn.shape, 2)) @ [1, 1j]
            shots.append(
                responses[acquisition][drawn] + self._get_noise(acquisition) * noise
            )
            starts.append(acquisition.get_starts())
            indices.append(np.full(acquisition.iterations, acquisition.index))
        order = np.argsort(np.concatenate(starts), kind="stable")
        values = np.concatenate(shots, axis=1)[:, order].ravel()
        return AcquiredChannel(
            BinMode.APPEND, np.concatenate(indices)[order].tolist(), values
        )

    def _get_noise(self, acquisition: Acquisition) -> float:
        """Gets the standard deviation of a shot's I, and of its Q, in the acquisition"""
        return get_noise(self.chip.readout_noise, acquisition)


@dataclass(frozen=True, eq=False)
class _Readout:
    """Acquisitions of a register's transmons at one instant, and the joint
    probabilities of their transmons' levels, an axis each in their order"""

    acquisitions: Tuple[Acquisition, ...]
    probabilities: np.ndarray

    def get_populations(self, acquisition: Acquisition) -> np.ndarray:
        """Gets the populations of the levels of the acquisition's transmon"""
        position = self.acquisitions.index(acquisition)
        others = tuple(
            axis for axis in range(len(self.acquisitions)) if axis != position
        )
        return self.probabilities.sum(axis=others)


_Event = Tuple[int, int, Union[Reset, Acquisition, Pulse]]


class _Group:
    """Transmons that couplers' pulses join, evolving as one register, each up to its
    own sample"""

    def __init__(
        self,
        elements: Sequence[str],
        transmons: Dict[str, Transmon],
        couplers: Dict[str, Coupler],
        clocks: Clocks,
        is_coupled: bool,
    ) -> None:
        """
        Args:
            elements: the transmons' names
            transmons: the chip's transmons, by name
            couplers: the chip's couplers, by name
            clocks: the clocks of the schedule they play
            is_coupled: whether two coupled transmons pick up their static ZZ while
                idle

        Raises:
            ValueError: the simulated chip has no transmon {name}
        """
        for name in elements:
            if name not in transmons:
                raise ValueError(f"the simulated chip has no transmon {name}")
        self._transmons = transmons
        self._couplers = couplers
        self._clocks = clocks
        self._register = Register(elements)
        self._samples = {name: 0 for name in elements}
        self._is_read = {name: False for name in elements}
        # each coupled pair's sample up to which it has its static ZZ
        self._coupled_samples = {
            name: 0
            for name in couplers
            if is_coupled and set(name.split("_")) <= set(elements)
        }

    def run(self, events: List[_Event]) -> Dict[Acquisition, _Readout]:
        """Evolves the transmons through the events and reads them out

        Args:
            events: what plays on them, in the order it starts

        Returns:
            the readout of each acquisition

        Raises:
            ValueError: the simulated chip has no coupler {name}
            ValueError: overlapping pulses drive {name}
            UnsupportedCircuitError: the simulated chip reads a transmon once between its
                resets, after what else plays on it
        """
        readouts: Dict[Acquisition, _Readout] = {}
        for position, (start, kind, event) in enumerate(events):
            if isinstance(event, Reset):
                self.reset(event)
            elif isinstance(event, Acquisition) and event not in readouts:
                together = [
                    other
                    for other_start, other_kind, other in events[position:]
                    if (other_start, other_kind) == (start, kind)
                ]
                readout = self.read(together)
                readouts.update({acquisition: readout for acquisition in together})
            elif isinstance(event, Pulse) and event.port.endswith(_FLUX_PORT):
                self.apply_flux_pulse(event)
            elif isinstance(event, Pulse):
                self.apply_drive(event)
        return readouts

    def reset(self, reset: Reset) -> None:
        """Takes the reset's transmons of the group to |0>

        Args:
            reset: the reset
        """
        for name in set(reset.elements) & set(self._samples):
            self._apply_static_zz_until(name, reset.start)
            self._register.reset(name)
            self._samples[name] = max(self._samples[name], reset.start)
            self._is_read[name] = False

    def read(self, acquisitions: List[Acquisition]) -> _Readout:
        """Reads the transmons of acquisitions that start at once

        Args:
            acquisitions: the acquisitions

        Returns:
            their readout

        Raises:
            UnsupportedCircuitError: the simulated chip reads a transmon once between its
                resets, after what else plays on it
        """
        elements = [acquisition.element for acquisition in acquisitions]
        for name in elements:
            if self._is_read[name] or elements.count(name) > 1:
                raise UnsupportedCircuitError(MEASURED_LAST)
            if acquisitions[0].start > self._samples[name]:
                self._wait_until(name, acquisitions[0].start)
            self._apply_static_zz_until(name, acquisitions[0].start)
            self._is_read[name] = True
        probabilities = self._register.get_probabilities(elements)
        return _Readout(tuple(acquisitions), probabilities)

    def apply_drive(self, pulse: Pulse) -> None:
        """Evolves a transmon through a pulse on its drive, at its clock's frequency and
        phase then

        Args:
            pulse: the pulse

        Raises:
            ValueError: overlapping pulses drive {name}
            UnsupportedCircuitError: the simulated chip reads a transmon once between its
                resets, after what else plays on it
        """
        name = pulse.port.split(":")[0]
        self._wait_until(name, pulse.start)
        self._apply_static_zz_until(name, pulse.start)
        transmon = self._transmons[name]
        frame = transmon.qubit.frequency
        frequency = self._clocks.get_frequency(pulse.clock, pulse.start)
        phase = self._get_clock_phase(pulse) - get_turns(frame, pulse.start)
        drive = transmon.get_drive(
            pulse.samples, pulse.key, frequency - frame, phase % 1.0
        )
        self._register.apply_local(name, drive)
        self._samples[name] = pulse.end

    def apply_flux_pulse(self, pulse: Pulse) -> None:
        """Evolves a coupler's transmons through its flux pulse, their pair's unitary,
        followed by each one's idling for its duration

        Args:
            pulse: the pulse

        Raises:
            ValueError: the simulated chip has no coupler {name}
            ValueError: overlapping pulses drive {name}
            UnsupportedCircuitError: the simulated chip reads a transmon once between its
                resets, after what else plays on it
        """
        name = pulse.port.split(":")[0]
        coupler = self._couplers.get(name)
        if coupler is None:
            raise ValueError(f"the simulated chip has no coupler {name}")
        elements = name.split("_")
        for element in elements:
            self._wait_until(element, pulse.start)
            self._apply_static_zz_until(element, pulse.start)
        frequency = self._clocks.get_frequency(pulse.clock, pulse.start)
        phase = get_turns(DOWNCONVERSION_FREQUENCY, pulse.start)
        phase -= self._get_clock_phase(pulse)
        unitary = coupler.get_unitary(
            pulse.samples,
            pulse.key,
            DOWNCONVERSION_FREQUENCY - frequency,
            phase % 1.0,
        )
        anharmonic = _get_anharmonic_energies(
            [self._transmons[element].qubit.anharmonicity for element in elements]
        )
        self._register.apply_unitary(
            elements,
            _to_transmon_frames(unitary, coupler.static_energies, anharmonic, pulse),
        )
        for element in elements:
            idle = self._transmons[element].get_idle(len(pulse.samples))
            self._register.apply_local(element, idle)
            self._samples[element] = pulse.end
        if name in self._coupled_samples:
            self._coupled_samples[name] = pulse.end

    def _wait_until(self, name: str, start: int) -> None:
        """Idles the transmon until the start of a pulse on it

        Raises:
            ValueError: overlapping pulses drive {name}
            UnsupportedCircuitError: the simulated chip reads a transmon once between its
                resets, after what else plays on it
        """
        if start < self._samples[name]:
            raise ValueError(f"overlapping pulses drive {name}")
        if self._is_read[name]:
            raise UnsupportedCircuitError(MEASURED_LAST)
        if start > self._samples[name]:
            idle = self._transmons[name].get_idle(start - self._samples[name])
            self._register.apply_local(name, idle)
            self._samples[name] = start

    def _apply_static_zz_until(self, name: str, sample: int) -> None:
        """Turns each pair of the transmon and a coupled one by their static ZZ up to the
        sample, from where it last did, as their coupling does while they idle"""
        for coupler, start in self._coupled_samples.items():
            if name in coupler.split("_") and sample > start:
                energies = self._couplers[coupler].static_energies
                turns = energies * (sample - start) * SAMPLE
                self._register.apply_unitary(
                    coupler.split("_"), np.diag(np.exp(-2j * math.pi * turns))
                )
                self._coupled_samples[coupler] = sample

    def _get_clock_phase(self, pulse: Pulse) -> float:
        """Gets the phase of the pulse's clock at its start, in turns"""
        return self._clocks.get_phases(pulse.clock, np.array([pulse.start]))[0]


def _get_couplers(chip: ChipConfig, currents: Dict[str, float]) -> Dict[str, Coupler]:
    """Gets the chip's couplers, by name, each parked by its SPI rack DAC's current"""
    return {
        name: Coupler(name, coupler, _get_coupled(chip, name), currents.get(name, 0.0))
        for name, coupler in chip.couplers.items()
    }


def _get_coupled(chip: ChipConfig, coupler: str) -> Tuple:
    """Gets the coupler's transmons, in the order its name gives them"""
    first, second = coupler.split("_")
    return chip.qubits[first], chip.qubits[second]


def _get_transmons(
    chip: ChipConfig, couplers: Dict[str, Coupler]
) -> Dict[str, Transmon]:
    """Gets the chip's transmons, by name, each at its frequency as its couplers' parking
    moves it from the chip's"""
    frequencies = {name: qubit.frequency for name, qubit in chip.qubits.items()}
    for name, coupler in couplers.items():
        for element, frequency in zip(name.split("_"), coupler.frequencies):
            frequencies[element] += frequency - chip.qubits[element].frequency
    return {
        name: Transmon(qubit.model_copy(update={"frequency": frequencies[name]}))
        for name, qubit in chip.qubits.items()
    }


def _get_groups(
    timeline: Timeline, couplers: Dict[str, Coupler], is_coupled: bool
) -> List[List[str]]:
    """Gets the transmons the timeline plays on or reads out, those that couplers'
    pulses join in one group, and, if coupled transmons pick up their static ZZ, those
    of each coupler

    Raises:
        ValueError: the simulated chip plays no pulse on {port}
    """
    groups: Dict[str, set] = {}
    for name in _get_elements(timeline):
        groups[name] = {name}
    pairs = [_get_pair(pulse) for pulse in timeline.pulses]
    pairs = [
        pair
        for pulse, pair in zip(timeline.pulses, pairs)
        if pulse.port.endswith(_FLUX_PORT)
    ]
    if is_coupled:
        pairs += [name.split("_") for name in couplers]
    for pair in pairs:
        if set(pair) <= set(groups):
            joined = set().union(*(groups[name] for name in pair))
            groups.update({name: joined for name in joined})
    unique = {id(group): group for group in groups.values()}
    return sorted(sorted(group) for group in unique.values())


def _get_elements(timeline: Timeline) -> List[str]:
    """Gets the transmons the timeline drives, couples or reads out

    Raises:
        ValueError: the simulated chip plays no pulse on {port}
    """
    elements = {acquisition.element for acquisition in timeline.acquisitions}
    for pulse in timeline.pulses:
        if pulse.port.endswith(_DRIVE_PORT):
            elements.add(pulse.port.split(":")[0])
        elif pulse.port.endswith(_FLUX_PORT):
            elements.update(_get_pair(pulse))
        elif not pulse.port.endswith(_READOUT_PORT):
            raise ValueError(f"the simulated chip plays no pulse on {pulse.port}")
    return sorted(elements)


def _get_pair(pulse: Pulse) -> List[str]:
    """Gets the transmons of the coupler a flux pulse plays on"""
    return pulse.port.split(":")[0].split("_")


def _get_events(elements: Sequence[str], timeline: Timeline) -> List[_Event]:
    """Gets the resets of the transmons, their acquisitions and the pulses on their
    drives and couplers, in the order they start, a reset before an acquisition before
    a pulse at the same sample"""
    events: List[_Event] = [
        (reset.start, 0, reset)
        for reset in timeline.resets
        if set(reset.elements) & set(elements)
    ]
    events += [
        (acquisition.start, 1, acquisition)
        for acquisition in timeline.acquisitions
        if acquisition.element in elements
    ]
    for pulse in timeline.pulses:
        if pulse.port.endswith(_DRIVE_PORT) and pulse.port.split(":")[0] in elements:
            events.append((pulse.start, 2, pulse))
        elif pulse.port.endswith(_FLUX_PORT) and _get_pair(pulse)[0] in elements:
            events.append((pulse.start, 2, pulse))
    events.sort(key=lambda event: event[:2])
    return events


def _get_anharmonic_energies(anharmonicities: Sequence[float]) -> np.ndarray:
    """Gets the energy of each of a pair's states from its transmons' anharmonicities
    alone, in hertz, in the order of PAIR_STATES"""
    return np.array(
        [
            sum(
                anharmonicity * level * (level - 1) / 2
                for anharmonicity, level in zip(anharmonicities, state)
            )
            for state in PAIR_STATES
        ]
    )


def _to_transmon_frames(
    unitary: np.ndarray,
    static: np.ndarray,
    anharmonic: np.ndarray,
    pulse: Pulse,
) -> np.ndarray:
    """Converts a pair's unitary of a flux pulse, in the frame of the parked pair's
    energies, to its transmons' frames, less what their idling over the pulse turns them
    by, which they idle through after it"""
    before = _get_state_turns(static + anharmonic, pulse.start)
    after = _get_state_turns(static, pulse.end)
    after = after + _get_state_turns(anharmonic, pulse.start)
    turned = np.exp(-2j * math.pi * after)[:, np.newaxis] * unitary
    return turned * np.exp(2j * math.pi * before)


def _get_state_turns(energies: np.ndarray, sample: int) -> np.ndarray:
    """Gets the turns of each state's energy up to the sample, modulo 1"""
    return np.array([get_turns(energy, sample) for energy in energies])


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


def _get_channels(timeline: Timeline) -> Dict[int, List[Acquisition]]:
    """Gets each channel's acquisitions

    Raises:
        ValueError: acquisition channel {channel} mixes bin modes, or acquires on more
            than one port and clock
    """
    channels: Dict[int, List[Acquisition]] = defaultdict(list)
    for acquisition in timeline.acquisitions:
        channels[acquisition.channel].append(acquisition)
    for channel, acquisitions in channels.items():
        if len({acquisition.bin_mode for acquisition in acquisitions}) > 1:
            raise ValueError(f"acquisition channel {channel} mixes bin modes")
        if len({(item.port, item.clock) for item in acquisitions}) > 1:
            raise ValueError(
                f"acquisition channel {channel} acquires on more than one port "
                "and clock, which the clusters' sequencers cannot tell apart"
            )
    return channels
