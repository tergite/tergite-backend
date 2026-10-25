# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025, 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

import numpy as np
from qiskit.pulse import Acquire

from app.libs.qiskit.qobj import PulseQobj
from app.libs.quantum_executor.base.experiment import NativeExperiment
from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from app.libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from app.libs.quantum_executor.qiskit.executor import (
    QiskitDynamicsExecutor,
    _QiskitDynMeasReturn,
    _to_xarray,
)
from app.libs.quantum_executor.utils.logger import ExperimentLogger
from app.services.scheduler.store import get_jobs_store
from app.tests.utils.env import TEST_RQ_REDIS_URL


class MockQiskitDynamicsExecutor(QiskitDynamicsExecutor):
    # how a job's estimated duration passes; the fake_clock fixture moves its clock instead
    wait = staticmethod(time.sleep)

    def preprocess(
        self, qobj: PulseQobj, job_id: str, results_folder: Path = None
    ) -> Tuple[float, str]:
        from settings import PREPROCESSED_JOB_POOL

        results_folder = PREPROCESSED_JOB_POOL

        results = super().preprocess(qobj, job_id, results_folder=results_folder)
        qobj_header = qobj.header.to_dict()
        duration = qobj_header.get("test_duration")
        return duration, results[1]

    def run(self, job_id: str, inputs_folder: Path = None) -> str:
        from settings import PREPROCESSED_JOB_POOL

        inputs_folder = PREPROCESSED_JOB_POOL
        start_timestamp = datetime.now()
        jobs_store = get_jobs_store(TEST_RQ_REDIS_URL)
        job = jobs_store.get_one(job_id)
        estimated_duration = 0
        if job.estimated_duration:
            estimated_duration = job.estimated_duration

        result = super().run(job_id, inputs_folder=inputs_folder)
        end_timestamp = datetime.now()
        actual_duration = (end_timestamp - start_timestamp).total_seconds()
        sleep_duration = estimated_duration - actual_duration
        if sleep_duration > 0:
            self.wait(sleep_duration)

        return result


class StubQiskitDynamicsExecutor(MockQiskitDynamicsExecutor):
    """Measures every acquired qubit at zero at once instead of simulating, which takes a
    2-qubit job about 9 seconds; for tests that do not check the measured counts"""

    def _run_native(
        self,
        experiment: NativeExperiment,
        /,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        meas_return = _QiskitDynMeasReturn.from_native_qobj_config(native_config)
        channels = {
            instruction.channel
            for _, instruction in experiment.schedule.instructions
            if isinstance(instruction, Acquire)
        }
        if meas_return == _QiskitDynMeasReturn.AVG:
            shape = (len(channels), 2)
        else:
            shape = (native_config.shots, len(channels), 2)
        return _to_xarray(data=np.zeros(shape), meas_return=meas_return)
