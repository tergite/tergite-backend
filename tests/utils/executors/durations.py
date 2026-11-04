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
"""Jobs that take the test_duration in their first circuit's metadata: preprocessing
estimates it, and running waits out what is left of it"""

import time
from datetime import datetime
from pathlib import Path

from pytest_mock import MockerFixture

from libs.quantum_executor.base import executor as base_executor
from libs.quantum_executor.base.circuits import CircuitJobParams

# how a job's duration passes; the fake_clock fixture moves its clock instead
wait = time.sleep
# each job's test_duration, from its preprocessing, which runs in the worker that runs it
_test_durations: dict[str, float] = {}


def use_test_durations(mocker: MockerFixture) -> None:
    """Makes the tasks preprocess and run jobs that take their test_duration"""
    mocker.patch(
        "services.scheduler.tasks.preprocess_circuits", new=_preprocess_circuits
    )
    mocker.patch("services.scheduler.tasks.run_job", new=_run_job)


def _preprocess_circuits(
    executor: base_executor.QuantumExecutor,
    params: CircuitJobParams,
    job_id: str,
    results_folder: Path,
) -> tuple[float, str]:
    _, results_file = base_executor.preprocess_circuits(
        executor, params, job_id=job_id, results_folder=results_folder
    )
    duration = params.circuits[0].metadata.get("test_duration")
    _test_durations[job_id] = duration
    return duration, results_file


def _run_job(
    executor: base_executor.QuantumExecutor, job_id: str, inputs_folder: Path
) -> str:
    started_at = datetime.now()
    results_file = base_executor.run_job(executor, job_id, inputs_folder=inputs_folder)
    elapsed_seconds = (datetime.now() - started_at).total_seconds()
    remaining_seconds = (_test_durations.get(job_id) or 0) - elapsed_seconds
    if remaining_seconds > 0:
        wait(remaining_seconds)
    return results_file
