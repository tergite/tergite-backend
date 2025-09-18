# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright Abdullah-Al Amin 2021
# (C) Copyright Axel Andersson 2022
# (C) Copyright David Wahlstedt 2022
# (C) Copyright Martin Ahindura 2024
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.


from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from pydantic import ValidationError
from qiskit.qobj import PulseQobj
from qiskit_ibm_provider.utils import json_decoder

from app.libs.quantum_executor.utils.connections import get_executor_lock
from app.libs.quantum_executor.utils.serialization import iqx_rld
from app.utils.queues import QueuePool
from app.utils.store import Collection
from settings import (
    DEFAULT_PREFIX,
    REDIS_CONNECTION,
)

from ...dtos import Job, JobFile, JobStatus
from ...exc import JobAlreadyCancelled, MalformedJob
from ...service import Stage
from ...utils import get_rq_job_id, log_job_failure, update_job_stage
from ..postprocessing import (
    logfile_postprocess,
    postprocessing_failure_callback,
    postprocessing_success_callback,
)
from .utils import get_executor

rq_queues = QueuePool(prefix=DEFAULT_PREFIX, connection=REDIS_CONNECTION)
executor = get_executor()


def job_execute(job_file: Path):
    print(f"Executing file {str(job_file)}")
    jobs_db = Collection[Job](REDIS_CONNECTION, schema=Job)
    job_id = job_file.stem

    try:
        with job_file.open() as f:
            job_file_obj = JobFile.model_validate_json(f.read())
            job_dict = job_file_obj.model_dump()

        job_id = job_dict["job_id"]
        update_job_stage(jobs_db, job_id=job_id, stage=Stage.EXEC_W)

        qobj = _decompress_qobj(job_dict["params"]["qobj"])

        # Just a locking mechanism to ensure jobs don't interfere with each other
        with get_executor_lock():
            # --- In-place decode complex values
            # [[a,b],[c,d],...] -> [a + ib,c + id,...]
            json_decoder.decode_pulse_qobj(qobj)
            print(datetime.now(), "IN API CALLING RUN_EXPERIMENTS")
            results_file = executor.run(PulseQobj.from_dict(qobj), job_id=job_id)

        if results_file:
            job: Job = jobs_db.get_one((job_id,))
            if job.status == JobStatus.CANCELLED:
                raise JobAlreadyCancelled("cancelled")

            rq_queues.logfile_postprocessing_queue.enqueue(
                logfile_postprocess,
                on_success=postprocessing_success_callback,
                on_failure=postprocessing_failure_callback,
                job_id=get_rq_job_id(job_id, Stage.PST_PROC_Q),
                args=(results_file,),
            )

            # update job in database
            update_job_stage(jobs_db, job_id=job_id, stage=Stage.PST_PROC_Q)

        # clean up
        job_file.unlink(missing_ok=True)
        print("Job executed successfully")
        return {"message": "ok"}

    except ValidationError as exp:
        print(f"{exp}")
        return {"message": f"malformed job: {exp}"}

    except JobAlreadyCancelled as exp:
        print(f"{exp}")
        return {"message": f"{exp}"}

    except MalformedJob as exp:
        print(f"Invalid job\nJob execution failed. Key error: {exp}")
        reason = f"{exp}"
        log_job_failure(jobs_db, job_id=job_id, reason=reason)
        return {"message": reason}

    except Exception as exp:
        print(f"Job failed\nJob execution failed. exp: {exp}")
        log_job_failure(
            jobs_db, job_id=job_id, reason="unexpected error during execution"
        )
        return {"message": "failed"}


def _decompress_qobj(qobj_dict: Dict[str, Any]) -> Dict[str, Any]:
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
