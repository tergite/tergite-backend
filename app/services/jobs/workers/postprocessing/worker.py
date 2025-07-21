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

import functools
import logging
from pathlib import Path
from typing import Any, Type

import numpy as np
import numpy.typing as npt
import redis
import rq.job
from sklearn.utils.extmath import safe_sparse_dot

import settings
from app.libs.device_parameters import (
    DeviceCalibration,
    get_backend_config,
    get_device_calibration_info,
)
from app.libs.quantum_executor.base.quantum_job import (
    MeasLvl,
    QuantumJob,
    discriminate_results,
    read_job_from_hdf5,
    xarray_to_list,
)
from app.services.jobs.dtos import Job, JobStatus, Stage
from app.services.jobs.utils import (
    log_job_failure,
    move_file,
    update_job_in_mss,
    update_job_results,
    update_job_stage,
)
from app.services.jobs.workers.postprocessing.exc import PostProcessingError
from app.utils.api import get_mss_client
from app.utils.store import Collection
from settings import (
    LOGFILE_DOWNLOAD_POOL_DIRNAME,
    REDIS_CONNECTION,
    STORAGE_PREFIX_DIRNAME,
    STORAGE_ROOT,
)

LOCALHOST = "localhost"


_POST_PROC_POOL_DIR = (
    STORAGE_ROOT / STORAGE_PREFIX_DIRNAME / LOGFILE_DOWNLOAD_POOL_DIRNAME
)


def logfile_postprocess(logfile: Path) -> str:
    print(f"Postprocessing logfile {str(logfile)}")

    job_id = logfile.stem
    jobs_db = Collection[Job](REDIS_CONNECTION, schema=Job)
    new_file = move_file(logfile, new_folder=_POST_PROC_POOL_DIR, ext=".hdf5")
    print(f"Moved the logfile to {str(new_file)}")

    update_job_stage(jobs_db, job_id=job_id, stage=Stage.PST_PROC_W)

    # The return value will be passed to postprocessing_success_callback
    print("Identified TQC storage file, reading file using storage file")
    job = read_job_from_hdf5(new_file)
    return postprocess_storage_file(job)


def _apply_linear_discriminator(
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


def postprocess_storage_file(
    job: QuantumJob, backend_name: str = settings.DEFAULT_PREFIX
) -> str:
    job_id = job.job_id
    jobs_db = Collection[Job](REDIS_CONNECTION, schema=Job)

    try:
        with get_mss_client() as mss_client:
            if job.meas_level == MeasLvl.DISCRIMINATED:
                backend_config = get_backend_config()
                calibration = get_device_calibration_info(
                    REDIS_CONNECTION, backend_config=backend_config
                )
                discriminator = functools.partial(
                    _apply_linear_discriminator, calibration
                )

                memory = discriminate_results(job, discriminator=discriminator)
                job = update_job_results(jobs_db, job_id=job_id, data=memory)
                update_job_in_mss(mss_client, job_id=job_id, payload=job)
            elif job.meas_level == MeasLvl.INTEGRATED:
                memory = xarray_to_list(job)
                job = update_job_results(jobs_db, job_id=job_id, data=memory)
                update_job_in_mss(mss_client, job_id=job_id, payload=job)
            else:
                raise NotImplementedError(
                    f"meas_level {job.meas_level} is not supported"
                )

        return job.job_id
    except Exception as exp:
        raise PostProcessingError(exp=exp, job_id=job.job_id)


def postprocessing_success_callback(
    _rq_job, _rq_connection, result: str, *args, **kwargs
):
    """Callback to invoke when postprocessing succeeds

    Args:
        _rq_job: the rq job
        _rq_connection: the redis connection
        result: the result from the worker handler
    """
    # From logfile_postprocess:
    job_id = result
    jobs_db = Collection[Job](REDIS_CONNECTION, schema=Job)

    job = update_job_stage(jobs_db, job_id=job_id, stage=Stage.FINAL_Q)
    with get_mss_client() as mss_client:
        if job.status == JobStatus.SUCCESSFUL:
            job = update_job_stage(jobs_db, job_id=job_id, stage=Stage.FINAL_W)
            print(f"Job with ID {job_id} has finished")
        else:
            print(f"Job {job_id}, has failed: aborting. Status: {job.status}")

        update_job_in_mss(mss_client, job_id=job_id, payload=job)


# job, connection, type, value, traceback
def postprocessing_failure_callback(
    _rq_job: rq.job.Job,
    _rq_connection: redis.Redis,
    _type: Type,
    value: Any,
    traceback: Any,
):
    """Callback to be called when postprocessing fails

    Args:
        _rq_job: the rq job
        _rq_connection: the redis connection
        _type: the error type
        value: the value passed to the callback from the handler
        traceback: the error traceback
    """
    with get_mss_client() as mss_client:
        if isinstance(value, PostProcessingError):
            jobs_db = Collection[Job](REDIS_CONNECTION, schema=Job)
            job_id = value.job_id

            logging.error(value.exp)
            job = log_job_failure(
                jobs_db,
                job_id=job_id,
                reason="error during post processing",
            )
            update_job_in_mss(mss_client, job_id=job_id, payload=job)
