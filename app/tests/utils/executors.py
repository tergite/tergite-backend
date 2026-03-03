# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Test utilities for the executors"""

import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

from qiskit.qobj import PulseQobj

from app.services.scheduler.store import get_jobs_store

from ...libs.quantum_executor.qiskit.executor import QiskitDynamicsExecutor
from ...libs.quantum_executor.quantify.executor import QuantifyExecutor
from .env import TEST_RQ_REDIS_URL


class MockQuantifyExecutor(QuantifyExecutor):
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

        result = super(self.__class__, self).run(job_id, inputs_folder=inputs_folder)
        end_timestamp = datetime.now()
        actual_duration = (end_timestamp - start_timestamp).total_seconds()
        sleep_duration = estimated_duration - actual_duration
        if sleep_duration > 0:
            time.sleep(sleep_duration)

        return result


class MockQiskitDynamicsExecutor(QiskitDynamicsExecutor):
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

        result = super(self.__class__, self).run(job_id, inputs_folder=inputs_folder)
        end_timestamp = datetime.now()
        actual_duration = (end_timestamp - start_timestamp).total_seconds()
        sleep_duration = estimated_duration - actual_duration
        if sleep_duration > 0:
            time.sleep(sleep_duration)

        return result
