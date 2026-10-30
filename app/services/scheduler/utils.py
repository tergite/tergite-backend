# This code is part of Tergite
#
# (C) Nicklas Botö, Fabian Forslund 2022
# (C) Copyright Martin Ahindura 2023, 2024
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
"""Utility functions for the scheduler service"""
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

import numpy as np
from numpy import typing as npt

from ...libs.device_parameters import BackendConfig, DeviceCalibration
from ...libs.quantum_executor.utils.serialization import iqx_rld
from ...libs.queues.dtos import (
    ExecutorOptions,
    Job,
    JobEvent,
    JobResult,
    JobStage,
    JobStatus,
    Stage,
    TimestampPair,
    Timestamps,
)
from ...utils.config import DeviceSettings
from ...utils.datetime import utc_now_str
from ...utils.exc import MssRejectedError
from ...utils.logging import err_logger
from ..external.mss import DeviceEvent, DeviceEventName, EventResponse, MssClient
from .store import JobsStore

if TYPE_CHECKING:
    from ...libs.quantum_executor.base.executor import QuantumExecutor


# a job supervisor log's line, e.g. [2026-01-01T00:00:00.000Z] ERROR: ..., in UTC
_JOB_LOG_FORMAT = "[%(asctime)s] %(levelname)s: %(message)s"
_STAGE_TIMESTAMPS_MAP: Dict[Stage, Tuple[Tuple[JobStage, JobEvent], ...]] = {
    Stage.REG_Q: (),
    Stage.REG_W: (("registration", "started"),),
    Stage.PRE_PROC_Q: (("registration", "finished"),),
    Stage.PRE_PROC_W: (("pre_processing", "started"),),
    Stage.EXEC_Q: (("pre_processing", "finished"),),
    Stage.EXEC_W: (("execution", "started"),),
    Stage.PST_PROC_Q: (("execution", "finished"),),
    Stage.PST_PROC_W: (("post_processing", "started"),),
    Stage.FINAL_Q: (("post_processing", "finished"), ("final", "started")),
    Stage.FINAL_W: (("final", "finished"),),
}
_STAGE_STATUS_MAP: Dict[Stage, JobStatus] = {
    Stage.REG_Q: JobStatus.PENDING,
    Stage.REG_W: JobStatus.PENDING,
    Stage.PRE_PROC_Q: JobStatus.PENDING,
    Stage.PRE_PROC_W: JobStatus.EXECUTING,
    Stage.EXEC_Q: JobStatus.PENDING,
    Stage.EXEC_W: JobStatus.EXECUTING,
    Stage.PST_PROC_Q: JobStatus.EXECUTING,
    Stage.PST_PROC_W: JobStatus.EXECUTING,
    Stage.FINAL_Q: JobStatus.EXECUTING,
    Stage.FINAL_W: JobStatus.SUCCESSFUL,
}


def get_executor_options(device_settings: DeviceSettings) -> ExecutorOptions:
    """Gets the options of the device's quantum executor from its settings"""
    backend_config = BackendConfig.from_toml(
        device_settings.backend_settings,
        seed_file=device_settings.calibration_seed,
    )
    return ExecutorOptions(
        executor_type=device_settings.executor_type,
        backend_name=backend_config.name,
        backend_config=backend_config,
        calibration_seed_file=device_settings.calibration_seed,
        quantify_config_file=device_settings.quantify_config_file,
        quantify_metadata_file=device_settings.quantify_metadata_file,
        should_restore_currents=device_settings.should_restore_currents,
        are_clusters_resettable=device_settings.is_cluster_reset_allowed,
        data_directory=device_settings.executor_data_dir,
        calibration_node_config=device_settings.calibration_node_config_file,
        calibration_device_config=device_settings.calibration_device_config_file,
        calibration_spi_config=device_settings.calibration_spi_config_file,
        redis_url=device_settings.tuner_redis_url,
    )


def get_executor(executor_options: ExecutorOptions) -> "QuantumExecutor":
    """Gets a new quantum executor of the given options, its instruments reset"""
    executor = _init_executor(executor_options, reset=True)
    err_logger.info(
        f"QuantumExecutor initialised for backend '{executor_options.backend_name}'.'"
    )
    return executor


def log_job_error(job_supervisor_log: Path, message: str) -> None:
    """Logs the error to the device's job supervisor log"""
    _get_job_logger(job_supervisor_log).error(message)


def move_file(file: Path, new_folder: Path, ext: str = "") -> Path:
    """Moves the file to a new folder

    Args:
        file: the file to move
        new_folder: the new folder to move to
        ext: the extension to attach to the final file

    Returns:
        the path to the new file
    """
    new_file_name = file.stem
    new_folder.mkdir(parents=True, exist_ok=True)
    new_file_path = (new_folder / new_file_name).with_suffix(ext)
    return file.replace(new_file_path)


def update_job_stage(jobs_store: JobsStore, job_id: str, stage: Stage) -> Job:
    """Updates the job's stage in the database

    This also updates the timestamps and the status of the job

    Args:
        jobs_store: the device's jobs
        job_id: the unique identifier of jobs
        stage: the stage to set on the job

    Returns:
        the updated job
    """
    job = jobs_store.get_one(job_id)

    current_timestamp = utc_now_str()
    timestamps = _get_next_timestamps(
        job, next_stage=stage, current_time=current_timestamp
    )
    status = _get_next_status(job, next_stage=stage)

    return jobs_store.update(
        job_id,
        {
            "status": status,
            "stage": stage,
            "timestamps": timestamps,
            "updated_at": current_timestamp,
        },
    )


def mark_job_failed(
    jobs_store: JobsStore, job_id: str, reason: str, job_supervisor_log: Path
) -> Job:
    """Marks the job failed for the reason, and logs it to the job supervisor log

    Args:
        jobs_store: the device's jobs
        job_id: the unique identifier of the job
        reason: the failure reason
        job_supervisor_log: the device's job supervisor log file

    Returns:
        the updated job with its failure status
    """
    job = jobs_store.update(
        job_id,
        {
            "status": JobStatus.FAILED,
            "failure_reason": reason,
            "updated_at": utc_now_str(),
        },
    )

    log_job_error(
        job_supervisor_log,
        f"Job {job_id} failed at {job.stage.verbose_name} due to {reason}",
    )

    return job


def update_job_results(
    jobs_store: JobsStore,
    job_id: str,
    data: List[List[str]],
    machine_root_url: str,
) -> Job:
    """Updates the results of the job and returns the updated job

    Args:
        jobs_store: the device's jobs
        job_id: the unique identifier of the job
        data: the discriminated results from the quantum job
        machine_root_url: the URL this device's API is reached at

    Returns:
        the updated job
    """
    return jobs_store.update(
        job_id,
        {
            "status": JobStatus.SUCCESSFUL,
            "result": JobResult(memory=data),
            "download_url": f"{machine_root_url}/logfiles/{job_id}",
            "updated_at": utc_now_str(),
        },
    )


def update_job_in_mss(
    mss_client: MssClient, payload: Job, job_supervisor_log: Path
) -> EventResponse:
    """Updates the job in MSS with the given payload

    Args:
        mss_client: the MSS client
        payload: the new updates to apply to the given job in MSS
        job_supervisor_log: the device's job supervisor log file, where a rejection is logged

    Returns:
        the response received after request to MSS

    Raises:
        RuntimeError: Public API returned {resp.status_code}
    """
    job_update_event = DeviceEvent(name=DeviceEventName.JOB_UPDATED, data=payload)
    try:
        resp = mss_client.send_event(
            job_update_event, error_prefix="error sending job to MSS: "
        )
    except MssRejectedError as exp:
        log_job_error(job_supervisor_log, f"{exp}")
        raise RuntimeError(f"Public API returned an error: {exp}")

    return resp


def apply_linear_discriminator(
    device_calibration: DeviceCalibration,
    qubit_idx: int,
    iq_points: npt.NDArray[np.complex128],
) -> npt.NDArray[np.int_]:
    """
    Fetches the linear discriminator from the backend definition

    Args:
        device_calibration: calibration data of the device
        qubit_idx: ID of the qubit to discriminate
        iq_points: IQ points from the measurement

    Returns:
        Discriminated 0 and 1 states as numpy array

    """
    # TODO: We are having two "qubit_id" (e.g. q12 = 0, q13 = 1)
    #  and we should have some more meaningful representation
    lda = device_calibration.discriminators["lda"][f"q{qubit_idx}"]
    coefficients = np.array([lda["coef_0"], lda["coef_1"]])
    intercept = np.array(lda["intercept"])

    iq_pairs = np.zeros((iq_points.shape[0], 2))
    iq_pairs[:, 0] = iq_points.real
    iq_pairs[:, 1] = iq_points.imag

    scores = iq_pairs @ coefficients.T + intercept

    return (scores.ravel() > 0).astype(np.int_)


def decompress_qobj(qobj_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Reverses the compression done on the qobj at the SDK level

    Before submission, the qobj dict was compressed to ease
    transportation. This compression is reversed here.

    Note that this decompression is done in-place

    Args:
        qobj_dict: the dict of the PulseQobj to decompress

    Returns:
        A QObject dict that is decompressed
    """
    # --- In-place RLD pulse library
    # [([a,b], 2),...] -> [[a,b],[a,b],...]
    for pulse in qobj_dict["config"]["pulse_library"]:
        pulse["samples"] = iqx_rld(pulse["samples"])

    return qobj_dict


def _init_executor(options: ExecutorOptions, reset: bool = False) -> "QuantumExecutor":
    """Initializes the executor

    Args:
        options: the executor options useful to initialize the executor
        reset: whether to reset the executor; default = False

    Returns:
        An initialized quantum executor
    """
    executor_type = options.executor_type
    backend_config = options.backend_config

    if executor_type == "qiskit_pulse_1q":
        # only import this if the executor is of the qiskit_pulse so
        # that the rest of the code is independent of qiskit
        from ...libs.quantum_executor.qiskit.executor import QiskitDynamicsExecutor

        return QiskitDynamicsExecutor.new_one_qubit(
            backend_config=backend_config,
            data_dir=options.data_directory,
            reset=reset,
        )

    elif executor_type == "qiskit_pulse_2q":
        # only import this if the executor is of the qiskit_pulse so
        # that the rest of the code is independent of qiskit
        from ...libs.quantum_executor.qiskit.executor import QiskitDynamicsExecutor

        return QiskitDynamicsExecutor.new_two_qubit(
            backend_config=backend_config,
            data_dir=options.data_directory,
            reset=reset,
        )

    # only import this if the executor is of the quantify so
    # that the rest of the code is independent of quantify
    from ...libs.quantum_executor.quantify.executor import QuantifyExecutor

    return QuantifyExecutor(
        quantify_config_file=options.quantify_config_file,
        quantify_metadata_file=options.quantify_metadata_file,
        backend_config=backend_config,
        reset=reset,
        should_restore_currents=options.should_restore_currents,
        are_clusters_resettable=options.are_clusters_resettable,
        data_dir=options.data_directory,
        calib_node_conf=options.calibration_node_config,
        calib_device_conf=options.calibration_device_config,
        calib_seed_file=options.calibration_seed_file,
        calib_spi_conf=options.calibration_spi_config,
        redis_url=options.redis_url,
    )


def _get_next_status(job: Job, next_stage: Stage) -> JobStatus:
    """Gets the next status given a job and the next stage

    Args:
        job: the quantum job
        next_stage: the next stage this job is to go to

    Returns:
        the next job status for that job
    """
    status = job.status
    if not status.is_terminal():
        status = _STAGE_STATUS_MAP[next_stage]
    return status


def _get_next_timestamps(job: Job, next_stage: Stage, current_time: str) -> Timestamps:
    """Gets the next timestamps for the given job and the next stage

    Args:
        job: the quantum job
        next_stage: the next stage this job is to go to
        current_time: the current timestamp as a string

    Returns:
        the next timestamps for that job
    """
    timestamps = job.timestamps or Timestamps()
    updated_pairs = {}
    for stage_name, event in _STAGE_TIMESTAMPS_MAP[next_stage]:
        pair = getattr(timestamps, stage_name) or TimestampPair()
        updated_pairs[stage_name] = pair.model_copy(update={event: current_time})
    return timestamps.model_copy(update=updated_pairs)


def _get_job_logger(job_supervisor_log: Path) -> logging.Logger:
    """Gets the logger that writes to the job supervisor log, which it opens at its first
    record"""
    logger = logging.getLogger(f"bcc.job_supervisor.{job_supervisor_log}")
    if not logger.handlers:
        formatter = logging.Formatter(_JOB_LOG_FORMAT)
        formatter.converter = time.gmtime
        formatter.default_time_format = "%Y-%m-%dT%H:%M:%S"
        formatter.default_msec_format = "%s.%03dZ"
        handler = logging.FileHandler(job_supervisor_log, delay=True)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # the job supervisor log alone, not the application's
        logger.propagate = False
    return logger
