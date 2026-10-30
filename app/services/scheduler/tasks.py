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
from pathlib import Path

from pydantic import ValidationError

from app.libs.qiskit.qobj import PulseQobj

from ...libs.device_parameters import DeviceCalibration
from ...libs.qiskit_providers.utils import json_decoder
from ...libs.quantum_executor.base.executor import preprocess_qobj, run_job
from ...libs.quantum_executor.base.quantum_job import (
    MeasLvl,
    discriminate_results,
    read_job_from_hdf5,
    xarray_to_list,
)
from ...libs.queues.dtos import Job, JobStatus, Stage
from ...utils.exc import JobAlreadyCancelled, PostProcessingError
from ..devices import get_device_calibration_info
from ..external.mss import DeviceEvent, DeviceEventName
from .dtos import JobFile
from .resources import DeviceResources
from .utils import (
    apply_linear_discriminator,
    decompress_qobj,
    log_job_failure,
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
    job_id = job.job_id
    jobs_store = device.jobs_store
    results_folder = device.settings.preprocessed_job_pool

    try:
        with job_file.open() as file:
            job_file_obj = JobFile.model_validate_json(file.read())
            job_dict = job_file_obj.model_dump()

        job_id: str = job_dict["job_id"]
        update_job_stage(jobs_store, job_id=job_id, stage=Stage.PRE_PROC_W)

        qobj = decompress_qobj(job_dict["params"]["qobj"])

        # --- In-place decode complex values
        # [[a,b],[c,d],...] -> [a + ib,c + id,...]
        json_decoder.decode_pulse_qobj(qobj)
        with device.executor_lock:
            duration, _ = preprocess_qobj(
                device.executor,
                PulseQobj.from_dict(qobj),
                job_id=job_id,
                results_folder=results_folder,
            )
        return jobs_store.update(job.job_id, {"estimated_duration": duration})
    except JobAlreadyCancelled as exp:
        logging.error(f"{exp}")
        raise exp

    except ValidationError as exp:
        _fail_job(device, job_id=job_id, reason=f"{exp}")
        raise exp

    except Exception as exp:
        logging.error(f"Job failed\nJob execution failed. exp: {exp}")
        _fail_job(device, job_id=job_id, reason="unexpected error during execution")
        raise exp


def execute(device: DeviceResources, job: Job) -> str:
    """Runs the job given the file containing compiled experiments

    Args:
        device: what the device runs on
        job: the job that is to be run

    Returns:
        the results file's path
    """
    job_id = job.job_id
    jobs_store = device.jobs_store
    preprocessing_dir = device.settings.preprocessed_job_pool

    try:
        update_job_stage(jobs_store, job_id=job_id, stage=Stage.EXEC_W)

        # Just a locking mechanism to ensure jobs don't interfere with each other
        with device.executor_lock:
            results_file = run_job(
                device.executor, job_id, inputs_folder=preprocessing_dir
            )

        job: Job = jobs_store.get_one(job_id)
        if job.status == JobStatus.CANCELLED:
            raise JobAlreadyCancelled("cancelled")

        logging.info("Job executed successfully")
        return results_file
    except JobAlreadyCancelled as exp:
        logging.error(f"{exp}")
        raise exp

    except Exception as exp:
        logging.error(f"Job failed\nJob execution failed. exp: {exp}")
        _fail_job(device, job_id=job_id, reason="unexpected error during execution")
        raise exp


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
    results_file_path = Path(results_file)

    job_id = job.job_id
    working_folder = device.settings.log_file_pool
    backend_name = device.executor_options.backend_name

    jobs_store = device.jobs_store
    try:
        new_file = move_file(results_file_path, new_folder=working_folder, ext=".hdf5")
        logging.info(f"Moved the logfile to {str(new_file)}")

        update_job_stage(jobs_store, job_id=job_id, stage=Stage.PST_PROC_W)

        quantum_job = read_job_from_hdf5(new_file)
        if quantum_job.meas_level == MeasLvl.DISCRIMINATED:
            calibration = get_device_calibration_info(
                device.bookings_db, backend_name=backend_name
            )
            discriminator = functools.partial(apply_linear_discriminator, calibration)

            memory = discriminate_results(
                quantum_job,
                discriminator=discriminator,
                calibration=calibration,
            )
            job = update_job_results(
                jobs_store,
                job_id=job_id,
                data=memory,
                machine_root_url=device.settings.machine_root_url,
            )
            update_job_in_mss(
                device.mss_client,
                payload=job,
                job_supervisor_log=device.settings.job_supervisor_log,
            )
        elif quantum_job.meas_level == MeasLvl.INTEGRATED:
            memory = xarray_to_list(quantum_job)
            job = update_job_results(
                jobs_store,
                job_id=job_id,
                data=memory,
                machine_root_url=device.settings.machine_root_url,
            )
            update_job_in_mss(
                device.mss_client,
                payload=job,
                job_supervisor_log=device.settings.job_supervisor_log,
            )
        else:
            raise NotImplementedError(
                f"meas_level {quantum_job.meas_level} is not supported"
            )

        return job.job_id
    except Exception as exp:
        raise PostProcessingError(exp=exp, job_id=job_id)


def recalibrate(device: DeviceResources) -> None:
    """Recalibrates the device, and sends MSS the calibration it found"""
    # ensure recalibration runs without interference
    with device.executor_lock:
        executor = device.executor
        results = executor.recalibrate()

        if isinstance(results, DeviceCalibration):
            logging.info("Updating MSS...")
            response = device.mss_client.send_event(
                DeviceEvent(
                    name=DeviceEventName.RECALIBRATED,
                    data=results,
                ),
                error_prefix="error sending recalibration info: ",
            )

            logging.info(
                f"calibration data updated in MSS with response: {response["status"]}"
            )


def _fail_job(device: DeviceResources, job_id: str, reason: str) -> Job:
    """Marks the job as failed for the given reason, and tells MSS"""
    job = log_job_failure(
        device.jobs_store,
        job_id=job_id,
        reason=reason,
        job_supervisor_log=device.settings.job_supervisor_log,
    )
    update_job_in_mss(
        device.mss_client,
        payload=job,
        job_supervisor_log=device.settings.job_supervisor_log,
    )
    return job
