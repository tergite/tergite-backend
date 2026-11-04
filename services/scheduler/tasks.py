# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright David Wahlstedt 2021, 2022, 2023
# (C) Copyright Abdullah Al Amin 2021, 2022
# (C) Copyright Axel Andersson 2022
# (C) Andreas Bengtsson 2020
# (C) Martin Ahindura 2023
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
"""Module containing the tasks to run on the job"""

import functools
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from pydantic import ValidationError

from libs.device_parameters import DeviceCalibration
from libs.quantum_executor.base.executor import preprocess_circuits, run_job
from libs.quantum_executor.base.quantum_job import (
    MeasLvl,
    discriminate_circuit_results,
    get_circuit_iq_memory,
    read_job_from_hdf5,
)
from libs.quantum_executor.base.quantum_job.dtos import MeasRet, QuantumJob
from libs.queues.dtos import (
    Job,
    JobResult,
    JobResultCircuit,
    JobStatus,
    Memory,
    Stage,
)
from utils.exc import (
    JobAlreadyCancelled,
    PostProcessingError,
    UnsupportedCircuitError,
)

from ..devices import get_device_calibration_info, save_device_calibration
from .dtos import JobFile
from .resources import DeviceResources
from .utils import (
    apply_linear_discriminator,
    mark_job_failed,
    move_file,
    update_job_in_mss,
    update_job_results,
    update_job_stage,
)


def preprocess(device: DeviceResources, job: Job, job_file: Path) -> Job:
    """Prepares the job for execution

    One of the most important thing it does is to set the estimated_duration of the job.
    This requires compiling the job to get the absolute timings of the schedules.

    Args:
        device: what the device runs on
        job: the job that is to be preprocessed
        job_file: the path to the job file

    Returns:
        the job, with its estimated duration
    """
    with _fail_on_error(device, job.job_id):
        job_file_content = _read_job_file(job_file)
        update_job_stage(device.jobs_store, job_id=job.job_id, stage=Stage.PRE_PROC_W)
        duration = _estimate_duration(device, job_file_content)
        return device.jobs_store.update(job.job_id, {"estimated_duration": duration})


def execute(device: DeviceResources, job: Job) -> str:
    """Runs the job given the file containing compiled experiments

    Args:
        device: what the device runs on
        job: the job that is to be run

    Returns:
        the results file's path
    """
    job_id = job.job_id
    with _fail_on_error(device, job_id):
        update_job_stage(device.jobs_store, job_id=job_id, stage=Stage.EXEC_W)

        # Just a locking mechanism to ensure jobs don't interfere with each other
        with device.executor_lock:
            results_file = run_job(
                device.executor,
                job_id,
                inputs_folder=device.settings.preprocessed_job_pool,
            )

        if device.jobs_store.get_one(job_id).status == JobStatus.CANCELLED:
            raise JobAlreadyCancelled("cancelled")

        logging.info("Job executed successfully")
        return results_file


def postprocess(device: DeviceResources, job: Job, results_file: str) -> str:
    """Processes the results got from running the experiments on the quantum computer

    Args:
        device: what the device runs on
        job: the quantum job that is to be post processed
        results_file: the path to the file containing the results from the experiments

    Returns:
        the job's ID
    """
    logging.info(f"Postprocessing logfile {results_file}")
    job_id = job.job_id
    jobs_store = device.jobs_store
    try:
        new_file = move_file(
            Path(results_file), new_folder=device.settings.log_file_pool, ext=".hdf5"
        )
        logging.info(f"Moved the logfile to {str(new_file)}")

        update_job_stage(jobs_store, job_id=job_id, stage=Stage.PST_PROC_W)

        quantum_job = read_job_from_hdf5(new_file)
        calibration = None
        if quantum_job.meas_level == MeasLvl.DISCRIMINATED:
            calibration = get_device_calibration_info(
                device.bookings_db, backend_name=device.executor_options.backend_name
            )
        job = update_job_results(
            jobs_store,
            job_id=job_id,
            result=_get_job_result(quantum_job, calibration),
            machine_root_url=device.settings.machine_root_url,
        )
        update_job_in_mss(device.bookings_db, device.mss_client, payload=job)
        return job.job_id
    except Exception as exp:
        raise PostProcessingError(exp=exp, job_id=job_id)


def recalibrate(device: DeviceResources) -> None:
    """Recalibrates the device, and saves and sends MSS the calibration it found

    Args:
        device: what the device runs on, its executor loaded
    """
    # ensure recalibration runs without interference
    with device.executor_lock:
        calibration = device.executor.recalibrate()
        if isinstance(calibration, DeviceCalibration):
            save_device_calibration(
                device.bookings_db,
                backend_name=device.executor_options.backend_name,
                calibration=device.executor.add_pulse_calibration(calibration),
                mss_client=device.mss_client,
            )


@contextmanager
def _fail_on_error(device: DeviceResources, job_id: str) -> Iterator[None]:
    """Fails the job, and tells MSS, on an error in the block but its cancellation; either
    way, it re-raises"""
    try:
        yield
    except JobAlreadyCancelled as exp:
        logging.error(f"{exp}")
        raise
    except (ValidationError, UnsupportedCircuitError) as exp:
        _fail_job(device, job_id=job_id, reason=f"{exp}")
        raise
    except Exception as exp:
        logging.error(f"Job failed\nJob execution failed. exp: {exp}")
        _fail_job(device, job_id=job_id, reason="unexpected error during execution")
        raise


def _read_job_file(job_file: Path) -> JobFile:
    """Reads the uploaded job file, which validates it"""
    with job_file.open() as file:
        return JobFile.model_validate_json(file.read())


def _estimate_duration(device: DeviceResources, job_file: JobFile) -> float:
    """Compiles the job's circuits on the executor, and gets the seconds they would take"""
    with device.executor_lock:
        duration, _ = preprocess_circuits(
            device.executor,
            job_file.params,
            job_id=job_file.job_id,
            results_folder=device.settings.preprocessed_job_pool,
        )
    return duration


def _get_job_result(
    quantum_job: QuantumJob, calibration: Optional[DeviceCalibration]
) -> JobResult:
    """Gets the job's result: its memory, and how it ran, so that a client reads the memory
    back without the job's payload

    Raises:
        NotImplementedError: meas_level {meas_level} is not supported
    """
    return JobResult(
        memory=_get_memory(quantum_job, calibration),
        shots=quantum_job.shots,
        meas_level=quantum_job.meas_level.value,
        meas_return="avg" if quantum_job.meas_return == MeasRet.AVERAGED else "single",
        circuits=[
            JobResultCircuit(name=readout.name, num_clbits=readout.num_clbits)
            for readout in quantum_job.readouts
        ],
    )


def _get_memory(
    quantum_job: QuantumJob, calibration: Optional[DeviceCalibration]
) -> Memory:
    """Gets the job's memory: each shot's register as hex, discriminated with the
    calibration, or its IQ points

    Raises:
        NotImplementedError: meas_level {meas_level} is not supported
    """
    if quantum_job.meas_level == MeasLvl.DISCRIMINATED:
        discriminator = functools.partial(apply_linear_discriminator, calibration)
        return discriminate_circuit_results(quantum_job, discriminator=discriminator)
    if quantum_job.meas_level == MeasLvl.INTEGRATED:
        return get_circuit_iq_memory(quantum_job)
    raise NotImplementedError(f"meas_level {quantum_job.meas_level} is not supported")


def _fail_job(device: DeviceResources, job_id: str, reason: str) -> Job:
    """Marks the job as failed for the given reason, and tells MSS"""
    job = mark_job_failed(
        device.jobs_store,
        job_id=job_id,
        reason=reason,
        job_supervisor_log=device.settings.job_supervisor_log,
    )
    update_job_in_mss(device.bookings_db, device.mss_client, payload=job)
    return job
