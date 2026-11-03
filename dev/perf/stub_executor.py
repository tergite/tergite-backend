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
"""The QuTiP executor with its simulation stubbed out, for dev/perf/stub_worker.py"""

from quantify_scheduler import Schedule

from libs.quantum_executor.base.circuits import CircuitExperiment
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from libs.quantum_executor.quantify import simulator
from libs.quantum_executor.quantify.qutip_executor import QutipExecutor
from libs.quantum_executor.utils.logger import ExperimentLogger


class StubExecutor(QutipExecutor):
    """Compiles and reads out as the QuTiP executor does, but reads every acquisition of
    the ground state instead of simulating the schedule"""

    def run_native(
        self,
        experiment: CircuitExperiment[Schedule],
        /,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        compiled = self._compiler.compile(
            schedule=experiment.schedule,
            config=self._quantum_device.generate_compilation_config(),
        )
        events = simulator._get_events(compiled, self._simulated)
        model = simulator._Model.create(self._simulated, sorted(events.elements))
        ground_state = model.get_ground_state()
        states = {start: ground_state for start, _, _, _ in events.acquisitions}
        return simulator._read_out(
            events.acquisitions, states, model, native_config.shots, self._rng
        )
