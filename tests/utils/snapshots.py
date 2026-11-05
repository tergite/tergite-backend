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
"""Snapshots of what compiled schedules play, as JSON

This module imports nothing of the backend's, so that dev/schedule_snapshots can load it
by its path into a checkout from before the refactor, to snapshot that checkout's
schedules.
"""

import hashlib
from typing import Any, Dict, List

import numpy as np

# the decimals samples and phase shifts are kept to
_DECIMALS = 10


def to_snapshot(
    timeline: Any, waveforms: Dict[str, List[List[float]]]
) -> Dict[str, Any]:
    """Gets the snapshot of a timeline, adding the waveforms of its pulses to the
    waveforms, by their names, so that pulses of the same samples share one; what starts
    together is in the order of its port or clock, which set order leaves to chance

    Args:
        timeline: the timeline of a compiled schedule, as get_timeline gives it
        waveforms: the waveforms of the snapshots so far, by their names

    Returns:
        the snapshot, which JSON can hold
    """
    pulses = []
    for pulse in sorted(timeline.pulses, key=lambda item: (item.start, item.port)):
        samples = [
            [round(float(sample.real), _DECIMALS), round(float(sample.imag), _DECIMALS)]
            for sample in pulse.samples
        ]
        name = hashlib.sha1(repr(samples).encode()).hexdigest()[:12]
        waveforms[name] = samples
        pulses.append(
            {
                "start": pulse.start,
                "port": pulse.port,
                "clock": pulse.clock,
                "waveform": name,
            }
        )
    return {
        "repetitions": timeline.repetitions,
        "frequencies": dict(sorted(timeline.frequencies.items())),
        "pulses": pulses,
        "clock_changes": [
            {
                "start": change.start,
                "clock": change.clock,
                "is_reset": change.is_reset,
                "phase_shift": round(change.phase_shift, _DECIMALS),
                "frequency": change.frequency,
                "is_frequency_set": change.is_frequency_set,
            }
            for change in sorted(
                timeline.clock_changes, key=lambda item: (item.start, item.clock)
            )
        ],
        "resets": [
            {"start": reset.start, "elements": list(reset.elements)}
            for reset in sorted(timeline.resets, key=lambda item: item.start)
        ],
        "acquisitions": [
            {
                "start": acquisition.start,
                "duration": acquisition.duration,
                "port": acquisition.port,
                "clock": acquisition.clock,
                "channel": acquisition.channel,
                "index": acquisition.index,
                "bin_mode": str(acquisition.bin_mode.value),
                "protocol": acquisition.protocol,
                "loops": [list(loop) for loop in acquisition.loops],
            }
            for acquisition in sorted(
                timeline.acquisitions, key=lambda item: (item.start, item.port)
            )
        ],
    }


def to_samples(waveform: List[List[float]]) -> np.ndarray:
    """Gets the complex samples of a snapshot's waveform

    Args:
        waveform: the waveform, each sample its real and imaginary parts

    Returns:
        the samples
    """
    return np.array([complex(real, imag) for real, imag in waveform])
