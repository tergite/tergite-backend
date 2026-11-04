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
"""The simulated chip with its physics stubbed out, for dev/perf/stub_worker.py"""
from typing import Dict

import numpy as np
from quantify_scheduler.schedules.schedule import ScheduleBase

from libs.quantum_executor.quantify.simulated_chip.engine import (
    _READOUT_PORT,
    AcquiredChannel,
    SimulatedChip,
    _get_port_pulses,
    _Readout,
)
from libs.quantum_executor.quantify.simulated_chip.timeline import Clocks, get_timeline
from libs.quantum_executor.quantify.simulated_chip.transmon import LEVELS


class StubChip(SimulatedChip):
    """Reads out as the simulated chip does, but every acquisition of the ground state,
    evolving nothing"""

    def run(self, compiled: ScheduleBase) -> Dict[int, AcquiredChannel]:
        """Reads out the compiled schedule's acquisitions of the ground state

        Args:
            compiled: the schedule, compiled for the clusters

        Returns:
            what each acquisition channel's bins hold, by channel
        """
        timeline = get_timeline(compiled)
        clocks = Clocks(timeline)
        ground = np.eye(LEVELS)[0]
        readouts = {
            acquisition: _Readout((acquisition,), ground)
            for acquisition in timeline.acquisitions
        }
        pulses = _get_port_pulses(timeline, _READOUT_PORT)
        responses = {
            acquisition: self._get_responses(acquisition, pulses, clocks)
            for acquisition in timeline.acquisitions
        }
        return self._read_out(timeline, readouts, responses)
