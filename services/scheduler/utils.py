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
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import numpy as np
from numpy import typing as npt
from sqlalchemy import Engine

from libs.device_parameters import BackendConfig, DeviceCalibration
from libs.quantum_executor.base.job_files import get_expt_metadata_file
from libs.queues.dtos import (
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
from utils.config import DeviceSettings
from utils.datetime import utc_now_str
from utils.exc import JobAlreadyCompleteError
from utils.logging import SharedRotatingFileHandler, err_logger

from ..external.mss import DeviceEvent, DeviceEventName, MssClient, send_mss_event
from .store import JobsStore

if TYPE_CHECKING:
    from libs.quantum_executor.base.executor import QuantumExecutor


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
    """Gets the options of the device's quantum executor from its settings

    Args:
        device_settings: the settings of the device

    Returns:
        the options of the device's quantum executor
    """
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
        is_cluster_reset_allowed=device_settings.is_cluster_reset_allowed,
        data_directory=device_settings.executor_data_dir,
        calibration_node_config=device_settings.calibration_node_config_file,
        calibration_device_config=device_settings.calibration_device_config_file,
        calibration_spi_config=device_settings.calibration_spi_config_file,
    )


def get_executor(executor_options: ExecutorOptions) -> "QuantumExecutor":
    """Gets a new quantum executor of the given options, its instruments reset

    Args:
        executor_options: the options of the executor

    Returns:
        the new quantum executor
    """
    executor = _init_executor(executor_options, should_reset=True)
    err_logger.info(
        f"QuantumExecutor initialised for backend '{executor_options.backend_name}'.'"
    )
    return executor


def log_job_error(device_settings: DeviceSettings, message: str) -> None:
    """Logs the error to the device's job supervisor log

    Args:
        device_settings: the settings of the device, which name its log and how it rotates
        message: the error message
    """
    _get_job_logger(device_settings).error(message)


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


def delete_job_files(
    device_settings: DeviceSettings, job_id: str, experiment_folder: Optional[Path]
) -> None:
    """Deletes the files the device keeps of the job: its upload, its preprocessed
    experiments, its results, and its experiments' folder in the executor's data, those
    that there are

    Args:
        device_settings: the settings of the device
        job_id: the unique identifier of the job
        experiment_folder: the folder of the job's experiments; None if it has none
    """
    job_files = (
        device_settings.job_upload_pool / job_id,
        get_expt_metadata_file(job_id, folder=device_settings.preprocessed_job_pool),
        device_settings.log_file_pool / f"{job_id}.hdf5",
    )
    for job_file in job_files:
        job_file.unlink(missing_ok=True)
    # removed only inside the executor's data, whatever the job's row says
    if experiment_folder is not None and experiment_folder.resolve().is_relative_to(
        device_settings.executor_data_dir.resolve()
    ):
        shutil.rmtree(experiment_folder, ignore_errors=True)


def update_job_stage(jobs_store: JobsStore, job_id: str, stage: Stage) -> Job:
    """Updates the job's stage in the database

    This also updates the timestamps, and the status of a job that has not ended

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

    return jobs_store.update(
        job_id,
        {
            "status": _STAGE_STATUS_MAP[stage],
            "stage": stage,
            "timestamps": timestamps,
            "updated_at": current_timestamp,
        },
    )


def mark_job_failed(
    jobs_store: JobsStore, job_id: str, reason: str, device_settings: DeviceSettings
) -> Optional[Job]:
    """Marks the job failed for the reason, and logs it to the job supervisor log, unless
    it has already ended

    Args:
        jobs_store: the device's jobs
        job_id: the unique identifier of the job
        reason: the failure reason
        device_settings: the settings of the device, whose job supervisor log it logs to

    Returns:
        the failed job, or None if it had already ended
    """
    try:
        job = jobs_store.mark_ended(
            job_id,
            {
                "status": JobStatus.FAILED,
                "failure_reason": reason,
                "updated_at": utc_now_str(),
            },
        )
    except JobAlreadyCompleteError:
        return None

    log_job_error(
        device_settings,
        f"Job {job_id} failed at {job.stage.verbose_name} due to {reason}",
    )

    return job


def update_job_results(
    jobs_store: JobsStore,
    job_id: str,
    result: JobResult,
    machine_root_url: str,
) -> Optional[Job]:
    """Marks the job successful with its results, unless it has already ended

    Args:
        jobs_store: the device's jobs
        job_id: the unique identifier of the job
        result: the job's memory, and how it ran
        machine_root_url: the URL this device's API is reached at

    Returns:
        the successful job, or None if it had already ended
    """
    try:
        return jobs_store.mark_ended(
            job_id,
            {
                "status": JobStatus.SUCCESSFUL,
                "result": result,
                "download_url": f"{machine_root_url}/logfiles/{job_id}",
                "updated_at": utc_now_str(),
            },
        )
    except JobAlreadyCompleteError:
        return None


def update_job_in_mss(db_engine: Engine, mss_client: MssClient, payload: Job) -> None:
    """Sends MSS the job, or keeps it for MSS until MSS can be reached

    Args:
        db_engine: the SQL database the events are kept in
        mss_client: the client to the device's MSS
        payload: the job to send
    """
    send_mss_event(
        db_engine,
        mss_client,
        DeviceEvent(name=DeviceEventName.JOB_UPDATED, data=payload),
    )


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


def _init_executor(
    options: ExecutorOptions, should_reset: bool = False
) -> "QuantumExecutor":
    """Initializes the executor

    Args:
        options: the executor options useful to initialize the executor
        should_reset: whether to reset the executor; default = False

    Returns:
        An initialized quantum executor
    """
    backend_config = options.backend_config
    from libs.quantum_executor.quantify.executor import QuantifyExecutor

    return QuantifyExecutor(
        quantify_config_file=options.quantify_config_file,
        quantify_metadata_file=options.quantify_metadata_file,
        backend_config=backend_config,
        should_reset=should_reset,
        should_restore_currents=options.should_restore_currents,
        is_cluster_reset_allowed=options.is_cluster_reset_allowed,
        data_dir=options.data_directory,
        calib_node_conf=options.calibration_node_config,
        calib_device_conf=options.calibration_device_config,
        calib_seed_file=options.calibration_seed_file,
        calib_spi_conf=options.calibration_spi_config,
    )


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


def _get_job_logger(device_settings: DeviceSettings) -> logging.Logger:
    """Gets the logger that writes to the device's job supervisor log, which it opens at its
    first record, rotating it as the settings first read for that log say"""
    job_supervisor_log = device_settings.job_supervisor_log
    logger = logging.getLogger(f"bcc.job_supervisor.{job_supervisor_log}")
    if not logger.handlers:
        formatter = logging.Formatter(_JOB_LOG_FORMAT)
        formatter.converter = time.gmtime
        formatter.default_time_format = "%Y-%m-%dT%H:%M:%S"
        formatter.default_msec_format = "%s.%03dZ"
        handler = SharedRotatingFileHandler(
            job_supervisor_log,
            max_bytes=device_settings.job_supervisor_log_max_bytes,
            backup_count=device_settings.job_supervisor_log_backup_count,
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # the job supervisor log alone, not the application's
        logger.propagate = False
    return logger
