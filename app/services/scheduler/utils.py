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

from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import requests
from numpy import typing as npt
from pydantic import BaseModel
from redis import Redis
from sklearn.utils.extmath import safe_sparse_dot

import settings
from settings import (
    BCC_MACHINE_ROOT_URL,
    MSS_MACHINE_ROOT_URL,
)

from ...libs.device_parameters import (
    DeviceCalibration,
    get_backend_config,
    initialize_backend,
)
from ...libs.quantum_executor.base.executor import QuantumExecutor
from ...libs.quantum_executor.qiskit.executor import QiskitDynamicsExecutor
from ...libs.quantum_executor.quantify.executor import QuantifyExecutor
from ...libs.quantum_executor.utils.serialization import iqx_rld
from ...libs.queues.dtos import (
    Job,
    JobEvent,
    JobResult,
    JobStage,
    JobStatus,
    LogLevel,
    QueueContext,
    Stage,
    Timestamps,
)
from ...utils.api import get_mss_client
from ...utils.datetime import utc_now_str
from ...utils.redis_store import Collection

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
_EXECUTOR: Optional[QuantumExecutor] = None


def get_queue_context(force_normal_queue: bool = False, **kwargs) -> QueueContext:
    """Generates a queue context from the given settings

    Args:
        force_normal_queue: the flag for whether to force the usage of the normal queue

    Returns:
        the queue context
    """
    return {
        "queue_prefix": settings.DEFAULT_PREFIX,
        "booking_db_url": settings.BOOKING_DB_URL,
        "jobs_store_url": settings.RQ_REDIS_URL,
        "force_normal_queue": force_normal_queue,
        "max_idle_time": settings.MAX_IDLE_TIME,
        "is_async": settings.IS_ASYNC,
        "postprocessing_folder": f"{settings.LOG_FILE_POOL}",
        "preprocessing_folder": f"{settings.PREPROCESSED_JOB_POOL}",
        "job_upload_folder": f"{settings.JOB_UPLOAD_POOL}",
        **kwargs,
    }


def get_executor(
    redis: Redis = settings.REDIS_CONNECTION,
    executor_type: str = settings.EXECUTOR_TYPE,
    quantify_config_file: str = settings.QUANTIFY_CONFIG_FILE,
    quantify_metadata_file: str = settings.QUANTIFY_METADATA_FILE,
    mss_url: str = settings.MSS_MACHINE_ROOT_URL,
) -> QuantumExecutor:
    """Gets the executor for running jobs

    It also initializes the backend before returning the executor

    Args:
        redis: the connection to the redis database
        executor_type: the executor type to return
        quantify_config_file: the path to the configuration file of the executor
        quantify_metadata_file: the path to the metadata file of the executor
        mss_url: the URL to MSS

    Returns:
        An initialized quantum executor
    """
    global _EXECUTOR
    if _EXECUTOR is None:
        backend_config = get_backend_config()

        if executor_type == "quantify":
            _EXECUTOR = QuantifyExecutor(
                quantify_config_file=quantify_config_file,
                quantify_metadata_file=quantify_metadata_file,
                backend_config=backend_config,
            )

        if executor_type == "qiskit_pulse_1q":
            _EXECUTOR = QiskitDynamicsExecutor.new_one_qubit(
                backend_config=backend_config
            )
            backend_config.calibration_config.discriminators = (
                _EXECUTOR.backend.train_discriminator()
            )

        if executor_type == "qiskit_pulse_2q":
            _EXECUTOR = QiskitDynamicsExecutor.new_two_qubit(
                backend_config=backend_config
            )
            backend_config.calibration_config.discriminators = (
                _EXECUTOR.backend.train_discriminator()
            )

        initialize_backend(
            redis,
            mss_client=get_mss_client(),
            mss_url=mss_url,
            backend_config=backend_config,
        )

    return _EXECUTOR


def reset_cached_executor():
    """Clears the cached executor resetting it to None"""
    global _EXECUTOR
    if _EXECUTOR:
        _EXECUTOR.close()
    _EXECUTOR = None


def log_job_msg(message: str, level: LogLevel = LogLevel.INFO) -> None:
    """Save message to job supervisor log file.

    Args:
        message (str): message to log
        level (LogLevel, optional): log level of the message. Defaults to LogLevel.INFO.
    """
    # FIXME: Why a custom logger. Can't we use the normal logging
    color: Tuple[str, str, str] = (
        "\033[0m",  # color end
        "\033[0;33m",  # yellow
        "\033[0;31m",  # red
    )

    formatted_time = utc_now_str()

    logstring: str = (
        f"{color[level.value]}[{formatted_time}] {level.name}: {message}{color[0]}\n"
    )

    with settings.JOB_SUPERVISOR_LOG.open("a") as destination:
        destination.write(logstring)


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


def get_rq_job_id(quantum_job_id: str, stage: Stage) -> str:
    """Constructs an rq job id given the quantum job id and job stage

    Args:
        quantum_job_id: the job id of the quantum job
        stage: the stage at which we are at in the processing chain

    Returns:
        a string to be used as rq job id
    """
    return f"{quantum_job_id}_{stage.name}"


def update_job_stage(jobs_db: Collection[Job], job_id: str, stage: Stage) -> Job:
    """Updates the job's stage in the database

    This also updates the timestamps and the status of the job

    Args:
        jobs_db: the collection containing jobs
        job_id: the unique identifier of jobs
        stage: the stage to set on the job

    Returns:
        the updated job
    """
    key = (job_id,)
    job: Job = jobs_db.get_one(key)

    current_timestamp = utc_now_str()
    timestamps = _get_next_timestamps(
        job, next_stage=stage, current_time=current_timestamp
    )
    status = _get_next_status(job, next_stage=stage)

    return jobs_db.update(
        key,
        {
            "status": status,
            "stage": stage,
            "timestamps": timestamps,
            "updated_at": current_timestamp,
        },
    )


def log_job_failure(jobs_db: Collection[Job], job_id: str, reason: str) -> Job:
    """Logs the job in the db as failed

    Args:
        jobs_db: the collection containing job items
        job_id: the unique identifier of the job
        reason: the failure reason

    Returns:
        the updated job with its failure status
    """
    job = jobs_db.update(
        (job_id,),
        {
            "status": JobStatus.FAILED,
            "failure_reason": reason,
            "updated_at": utc_now_str(),
        },
    )

    log_job_msg(
        f"Job {job_id} failed at {job.stage.verbose_name} due to {reason}",
        level=LogLevel.ERROR,
    )

    return job


def update_job_results(
    jobs_db: Collection[Job], job_id: str, data: List[List[str]]
) -> Job:
    """Updates the results of the job and returns the updated job

    Args:
        jobs_db: the collection containing job items
        job_id: the unique identifier of the job
        data: the discriminated results from the quantum job

    Returns:
        the updated job
    """
    return jobs_db.update(
        (job_id,),
        {
            "status": JobStatus.SUCCESSFUL,
            "result": JobResult(memory=data),
            "download_url": f"{BCC_MACHINE_ROOT_URL}/logfiles/{job_id}",
            "updated_at": utc_now_str(),
        },
    )


def update_job_in_mss(
    mss_client: requests.Session, job_id: str, payload: Union[dict, Job]
) -> requests.Response:
    """Updates the job in MSS with the given payload

    Args:
        mss_client: the requests.Session that can query MSS
        job_id: the ID of the job
        payload: the new updates to apply to the given job in MSS

    Returns:
        the requests.Response received after request to MSS

    Raises:
        RuntimeError: Public API returned {resp.status_code}
    """
    data = payload
    if isinstance(payload, BaseModel):
        data = payload.model_dump(exclude_unset=True, mode="json")

    url = f"{MSS_MACHINE_ROOT_URL}/jobs/{job_id}"
    resp = mss_client.put(url, json=data)

    if not resp.ok:
        try:
            message = resp.json()
        except JSONDecodeError:
            message = resp.text

        log_job_msg(
            f"failed to submit job to MSS\nstatus:{resp.status_code}\nresponse:{message}",
            level=LogLevel.ERROR,
        )
        raise RuntimeError(f"Public API returned {resp.status_code}")

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
    discriminator_ = device_calibration.discriminators["lda"]
    # TODO: We are having two "qubit_id" (e.g. q12 = 0, q13 = 1)
    #  and we should have some more meaningful representation
    qubit_id_ = f"q{qubit_idx}"

    coefficients = np.array(
        [
            discriminator_[qubit_id_]["coef_0"],
            discriminator_[qubit_id_]["coef_1"],
        ]
    )
    intercept = np.array(discriminator_[qubit_id_]["intercept"])

    data = np.zeros((iq_points.shape[0], 2))
    data[:, 0] = iq_points.real
    data[:, 1] = iq_points.imag

    scores = safe_sparse_dot(data, coefficients.T, dense_output=True) + intercept

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
    timestamps = job.timestamps
    if timestamps is None:
        timestamps = Timestamps()

    new_timestamps = {
        stage_name: {timestamp_label: current_time}
        for stage_name, timestamp_label in _STAGE_TIMESTAMPS_MAP[next_stage]
    }

    return timestamps.with_updates(new_timestamps)
