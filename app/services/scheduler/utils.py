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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from numpy import typing as npt
from redis import Redis
from sklearn.utils.extmath import safe_sparse_dot

import settings

from ...libs.device_parameters import (
    BackendConfig,
    DeviceCalibration,
    get_backend_config,
    save_all_device_params,
)
from ...libs.quantum_executor.base.executor import QuantumExecutor
from ...libs.quantum_executor.qiskit.executor import QiskitDynamicsExecutor
from ...libs.quantum_executor.quantify.executor import QuantifyExecutor
from ...libs.quantum_executor.utils.serialization import iqx_rld
from ...libs.queues.dtos import (
    ExecutorOptions,
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
from ...utils.datetime import utc_now_str
from ...utils.redis_store import Collection
from ..external.mss.dtos import DeviceEvent, DeviceEventName, EventResponse
from ..external.mss.service import (
    AsyncMssClientPipe,
    MssClientPipe,
)

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
            "download_url": f"{settings.BCC_MACHINE_ROOT_URL}/logfiles/{job_id}",
            "updated_at": utc_now_str(),
        },
    )


def update_job_in_mss(mss_client_pipe: MssClientPipe, payload: Job) -> EventResponse:
    """Updates the job in MSS with the given payload

    Args:
        mss_client_pipe: the pipe connected to the MSS client
        payload: the new updates to apply to the given job in MSS

    Returns:
        the response received after request to MSS

    Raises:
        RuntimeError: Public API returned {resp.status_code}
    """
    job_update_event = DeviceEvent(name=DeviceEventName.JOB_UPDATED, data=payload)
    try:
        resp = mss_client_pipe.send_event(
            job_update_event, error_prefix="error sending job to MSS: "
        )
    except ValueError as exp:
        log_job_msg(f"{exp}", level=LogLevel.ERROR)
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


def init_executor(options: ExecutorOptions, reset: bool = False) -> QuantumExecutor:
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
        return QiskitDynamicsExecutor.new_one_qubit(
            backend_config=backend_config, reset=reset
        )

    elif executor_type == "qiskit_pulse_2q":
        return QiskitDynamicsExecutor.new_two_qubit(
            backend_config=backend_config, reset=reset
        )

    return QuantifyExecutor(
        quantify_config_file=options.quantify_config_file,
        quantify_metadata_file=options.quantify_metadata_file,
        backend_config=backend_config,
        reset=reset,
        should_restore_currents=options.should_restore_currents,
        are_clusters_resettable=options.are_clusters_resettable,
    )


def get_recalibration_job_id(context: QueueContext) -> str:
    """Gets the rq job id for the recalibration job

    Args:
        context: the context required when running a job on a queue

    Returns:
        the rq job id for the idle timer job
    """
    queue_prefix = context["queue_prefix"]
    return f"{queue_prefix}_recalibration_scheduler"


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


def _get_redis_url(redis: Redis) -> str:
    """Gets the redis URL from the redis connection

    Args:
        redis: the redis connection

    Returns:
        the redis URL  "redis(s)://username:password@host:port/dbname"

    Raises:
        ValueError: unix connection is not supported
    """
    conn_kwargs = redis.connection_pool.connection_kwargs
    auth_str = ""
    scheme = "redis"
    db = conn_kwargs["db"]
    host = conn_kwargs["host"]
    port = conn_kwargs["port"]
    if "username" in conn_kwargs:
        auth_str = f"{conn_kwargs['username']}"
    if "password" in conn_kwargs:
        auth_str = f":{conn_kwargs['password']}"
    if auth_str != "":
        auth_str = f"{auth_str}@"

    if conn_kwargs.get("ssl"):
        scheme = "rediss"

    return f"{scheme}://{auth_str}{host}:{port}/{db}"
