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
"""The readout of a simulated chip: what an acquisition integrates of its resonator's
response to the readout pulses, with its transmon in each level"""

import math
from typing import Iterable

import numpy as np

from libs.device_parameters.dtos import ChipResonator

from .timeline import Acquisition, Clocks, Pulse
from .transmon import LEVELS

# the integration time, in samples, at which a shot's noise is the chip's readout noise
NOISE_SAMPLES = 1000


def get_transmission(
    resonator: ChipResonator,
    frequencies: np.ndarray,
    level: int,
    amplitudes: np.ndarray,
) -> np.ndarray:
    """Gets the resonator's transmission, a hanger's, at each frequency and readout
    amplitude, its transmon in the level

    Args:
        resonator: the chip's parameters of the resonator
        frequencies: the readout's frequencies, in hertz
        level: its transmon's level
        amplitudes: the readout's amplitudes

    Returns:
        the complex transmission at each
    """
    dressed = resonator.frequency + level * resonator.dispersive_shift
    pull = 1 / (1 + (np.asarray(amplitudes) / resonator.critical_amplitude) ** 2)
    dip = resonator.bare_frequency + (dressed - resonator.bare_frequency) * pull
    loaded = 1 / (1 / resonator.internal_quality + 1 / resonator.coupling_quality)
    detuning = 2j * loaded * (np.asarray(frequencies) - dip) / dip
    hanger = 1 - (loaded / resonator.coupling_quality) / (1 + detuning)
    return resonator.amplitude * np.exp(1j * resonator.phase) * hanger


def get_level_responses(
    acquisition: Acquisition,
    pulses: Iterable[Pulse],
    clocks: Clocks,
    resonator: ChipResonator,
) -> np.ndarray:
    """Gets what the acquisition integrates with its transmon in each level, the mean
    over its window of each readout pulse's envelope times the resonator's transmission,
    demodulated at the acquisition's clock, as the clusters return it

    Args:
        acquisition: the acquisition
        pulses: the pulses on its port
        clocks: the clocks of its timeline
        resonator: the chip's parameters of its resonator

    Returns:
        the complex response of each level
    """
    window = np.arange(acquisition.start, acquisition.start + acquisition.duration)
    demodulation = clocks.get_phases(acquisition.clock, window)
    responses = np.zeros(LEVELS, complex)
    for pulse in pulses:
        first, last = max(pulse.start, window[0]), min(pulse.end, window[-1] + 1)
        if first >= last:
            continue
        samples = pulse.samples[first - pulse.start : last - pulse.start]
        overlap = window[first - window[0] : last - window[0]]
        turns = (
            clocks.get_phases(pulse.clock, overlap) - demodulation[overlap - window[0]]
        )
        carried = samples * np.exp(2j * math.pi * turns)
        frequency = clocks.get_frequency(pulse.clock, first)
        for level in range(LEVELS):
            transmission = get_transmission(
                resonator, np.array(frequency), level, np.abs(samples)
            )
            responses[level] += np.sum(carried * transmission)
    return responses / acquisition.duration


def get_noise(readout_noise: float, acquisition: Acquisition) -> float:
    """Gets the standard deviation of a shot's I, and of its Q, in the acquisition

    Args:
        readout_noise: the chip's, of an integration over NOISE_SAMPLES
        acquisition: the acquisition

    Returns:
        the standard deviation
    """
    return readout_noise * math.sqrt(NOISE_SAMPLES / acquisition.duration)
