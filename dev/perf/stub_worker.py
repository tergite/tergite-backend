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
"""Runs main.py with the qiskit executor's simulation stubbed out, so that the performance
baselines measure the scheduler and not the physics; dev/perf/record.sh runs the heavy worker
with it

The qiskit executor is swapped before main.py builds one, so BCC_ENV_FILE must name the
device's env file: settings is read on import, before main.py could set it.
"""
import numpy as np
from qiskit.pulse import Acquire

import app.libs.quantum_executor.qiskit.executor as qiskit_executor
from app.libs.quantum_executor.base.experiment import NativeExperiment
from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from app.libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from app.libs.quantum_executor.utils.logger import ExperimentLogger


class StubExecutor(qiskit_executor.QiskitDynamicsExecutor):
    """Preprocesses and postprocesses as the qiskit executor does, but measures every
    acquired qubit at zero at once instead of simulating it"""

    def _run_native(
        self,
        experiment: NativeExperiment,
        /,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        meas_return = qiskit_executor._QiskitDynMeasReturn.from_native_qobj_config(
            native_config
        )
        channels = {
            instruction.channel
            for _, instruction in experiment.schedule.instructions
            if isinstance(instruction, Acquire)
        }
        if meas_return == qiskit_executor._QiskitDynMeasReturn.AVG:
            shape = (len(channels), 2)
        else:
            shape = (native_config.shots, len(channels), 2)
        return qiskit_executor._to_xarray(data=np.zeros(shape), meas_return=meas_return)


qiskit_executor.QiskitDynamicsExecutor = StubExecutor

if __name__ == "__main__":
    import main

    main.main()
