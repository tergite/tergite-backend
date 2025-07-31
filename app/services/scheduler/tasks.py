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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple, Type

import redis
import rq.job
from pydantic import ValidationError
from qiskit.qobj import PulseQobj
from rq import Repeat, get_current_job

from ...libs.device_parameters import get_backend_config, get_device_calibration_info
from ...libs.qiskit_providers.utils import json_decoder
from ...libs.quantum_executor.base.quantum_job import (
    MeasLvl,
    discriminate_results,
    read_job_from_hdf5,
    xarray_to_list,
)
from ...libs.quantum_executor.utils.connections import get_executor_lock
from ...libs.queues.dtos import (
    Job,
    JobFile,
    JobStatus,
    QueueContext,
    Stage,
    StorageID,
)
from ...utils.api import get_mss_client
from ...utils.datetime import get_utc_now
from ...utils.exc import JobAlreadyCancelled, PostProcessingError
from ...utils.redis_store import Collection
from ...utils.rq import cancel_rq_job
from ..booking.models import Booking
from ..booking.service import get_booking, get_next_booking
from ..booking.store import get_bookings_sql_engine
from .store import get_jobs_store
from .utils import (
    apply_linear_discriminator,
    decompress_qobj,
    get_executor,
    get_rq_job_id,
    log_job_failure,
    move_file,
    update_job_in_mss,
    update_job_results,
    update_job_stage,
)


def preprocess(
    job: Job,
    context: QueueContext,
    job_file: Path,
    booking_id: Optional[str] = None,
) -> Tuple[str, QueueContext]:
    """Prepares the job for execution

    One of the most important thing it does is to set the estimated_duration of the job.
    This requires compiling the job to get the absolute timings of the schedules.

    if force_normal_queue is True:
        - Push the job to the waitlist

    Args:
        job: the job that is to be preprocessed
        context: the context required when running a job on a queue
        job_file: the path to the job file
        booking_id: the unique identifier of the current booking if any

    Returns:
        the pair of the updated job's job ID and the context
    """
    job_id = job.job_id
    jobs_store = get_jobs_store(url=context["jobs_store_url"])
    results_folder = Path(context["preprocessing_folder"])

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
        executor = get_executor()
        duration, _ = executor.preprocess(
            PulseQobj.from_dict(qobj), job_id=job_id, results_folder=results_folder
        )
        job = jobs_store.update(job.job_id, {"estimated_duration": duration})

        if booking_id is None:
            return _try_enqueue_on_normal_queue(job, context)
        else:
            return _try_enqueue_on_booked_queue(
                job, context, booking_id=booking_id, is_from_waitlist=False
            )
    except JobAlreadyCancelled as exp:
        logging.error(f"{exp}")
        raise exp

    except ValidationError as exp:
        log_job_failure(jobs_store, job_id=job_id, reason=f"{exp}")
        raise exp

    except Exception as exp:
        logging.error(f"Job failed\nJob execution failed. exp: {exp}")
        log_job_failure(
            jobs_store, job_id=job_id, reason="unexpected error during execution"
        )
        raise exp


def post_booking_cleanup(booking_id: str, context: QueueContext):
    """Cleans up after the booking of the given booking_id

    It should cancel the idle time interval job and allow for all the jobs on the booked_execution queues to clear out.
    It should also move all jobs that are short enough on the waitlist to the execution queue.

    Args:
        booking_id: the unique identifier of the booking
        context: the variables in the environment in which the job is to run
    """
    booking_db_url = context["booking_db_url"]

    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)
    booking = get_booking(bookings_sql_engine, Booking.id == booking_id)
    redis_connection = get_current_job().connection

    if booking is None:
        logging.error(f"no booking found with id {booking_id}")
        return

    # clear the idleness timer
    cancel_rq_job(redis_connection, job_id=booking.idle_timer_id)

    # push to normal execution queue all jobs that have duration
    # small enough to run before next booking
    _pop_waitlist_to_normal_queue(context)


def reset_idleness_timer(
    booking_id: str,
    end_utc: datetime,
    context: QueueContext,
    restart_in: float = 0,
    max_idle_time: Optional[float] = None,
):
    """Refreshes the idle timer for the booking of given booking_id

    If end_utc is in the past, no, new timer will be created but the old one will be removed

    Args:
        booking_id: the unique identifier of the booking whose idleness timer is to be reset
        end_utc: the timestamp beyond which the timer should not run
        restart_in: the number of seconds when the timer should restart; default = 0 i.e. immediately
        context: the environment in which the jobs are running
        max_idle_time: the maximum acceptable time for queue to be idle

    Returns:
        the refreshed rq.job.Job that is constructed and enqueued to track the time or None if end_utc is in the past
    """
    from .queues import get_general_queue

    queue_prefix = context["queue_prefix"]
    is_async = context["is_async"]
    booking_db_url = context["booking_db_url"]
    if max_idle_time is None:
        max_idle_time = context["max_idle_time"]

    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)
    redis_connection = get_current_job().connection
    general_queue = get_general_queue(
        prefix=queue_prefix, connection=redis_connection, is_async=is_async
    )

    booking = get_booking(bookings_sql_engine, Booking.id == booking_id)
    if booking is None:
        logging.error(f"no booking found with id {booking_id}")
        return None

    cancel_rq_job(redis_connection, job_id=booking.idle_timer_id)

    duration = (end_utc - get_utc_now()).total_seconds() - restart_in
    if duration < max_idle_time:
        # don't restart timer if duration is less than the max_idle_time
        return None

    repeat = None
    # no repetitions if the max_idle_time is zero. Just run once and stop.
    if max_idle_time > 0:
        repetitions = int(duration / max_idle_time)
        repeat = Repeat(times=repetitions, interval=max_idle_time)

    general_queue.enqueue_in(
        timedelta(seconds=restart_in),
        _pop_waitlist_to_booking,
        context=context,
        # rq specific kwargs
        job_id=booking.idle_timer_id,
        repeat=repeat,
    )
    return None


def execute(
    job: Job,
    context: QueueContext,
) -> Tuple[str, QueueContext, Path]:
    """Runs the job given the file containing compiled experiments

    Args:
        job: the job that is to be run
        context: the context required when running a job on a queue

    Returns:
        the tuple of the updated job's job ID and the context and the results file path
    """
    from .queues import get_postprocessing_queue

    job_id = job.job_id
    connection = get_current_job().connection
    queue_prefix = context["queue_prefix"]
    is_async = context["is_async"]
    jobs_store = get_jobs_store(url=context["jobs_store_url"])
    preprocessing_dir = Path(context["preprocessing_folder"])

    try:
        update_job_stage(jobs_store, job_id=job_id, stage=Stage.EXEC_W)

        # Just a locking mechanism to ensure jobs don't interfere with each other
        with get_executor_lock():
            executor = get_executor()
            results_file = executor.run(job_id, inputs_folder=preprocessing_dir)

        job: Job = jobs_store.get_one((job_id,))
        if job.status == JobStatus.CANCELLED:
            raise JobAlreadyCancelled("cancelled")

        postproc_queue = get_postprocessing_queue(
            prefix=queue_prefix, connection=connection, is_async=is_async
        )
        job = update_job_stage(jobs_store, job_id=job_id, stage=Stage.PST_PROC_Q)
        postproc_queue.enqueue(
            job,
            context,
            results_file=results_file,
            job_id=get_rq_job_id(job_id, Stage.PST_PROC_Q),
        )

        # clean up
        logging.info("Job executed successfully")
        return job.job_id, context, results_file
    except JobAlreadyCancelled as exp:
        logging.error(f"{exp}")
        raise exp

    except Exception as exp:
        logging.error(f"Job failed\nJob execution failed. exp: {exp}")
        print(f"Job failed\nJob execution failed. exp: {exp}")
        log_job_failure(
            jobs_store, job_id=job_id, reason="unexpected error during execution"
        )
        raise exp


def postprocess(
    job: "Job", context: "QueueContext", results_file: Path
) -> Tuple[str, "QueueContext"]:
    """Processes the results got from running the experiments on the quantum computer

    Args:
        job: the quantum job that is to be post processed
        context: the context in the queue
        results_file: the path to the file containing the results from the experiments

    Returns:
        the tuple of job_id and the context
    """
    logging.info(f"Postprocessing logfile {str(results_file)}")

    job_id = job.job_id
    working_folder = Path(context["postprocessing_folder"])

    jobs_store = get_jobs_store(url=context["jobs_store_url"])
    new_file = move_file(results_file, new_folder=working_folder, ext=".hdf5")
    logging.info(f"Moved the logfile to {str(new_file)}")

    update_job_stage(jobs_store, job_id=job_id, stage=Stage.PST_PROC_W)

    # The return value will be passed to postprocessing_success_callback
    print("Identified TQC storage file, reading file using storage file")
    quantum_job = read_job_from_hdf5(new_file)

    try:
        with get_mss_client() as mss_client:
            if quantum_job.meas_level == MeasLvl.DISCRIMINATED:
                calibration = get_device_calibration_info()
                discriminator = functools.partial(
                    apply_linear_discriminator, calibration
                )

                memory = discriminate_results(quantum_job, discriminator=discriminator)
                job = update_job_results(jobs_store, job_id=job_id, data=memory)
                update_job_in_mss(mss_client, job_id=job_id, payload=job)
            elif quantum_job.meas_level == MeasLvl.INTEGRATED:
                memory = xarray_to_list(quantum_job)
                job = update_job_results(jobs_store, job_id=job_id, data=memory)
                update_job_in_mss(mss_client, job_id=job_id, payload=job)
            else:
                raise NotImplementedError(
                    f"meas_level {job.meas_level} is not supported"
                )

        return job.job_id, context
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
    jobs_store = Collection[Job](_rq_connection, schema=Job)

    job = update_job_stage(jobs_store, job_id=job_id, stage=Stage.FINAL_Q)
    with get_mss_client() as mss_client:
        if job.status == JobStatus.SUCCESSFUL:
            job = update_job_stage(jobs_store, job_id=job_id, stage=Stage.FINAL_W)
            print(f"Job with ID {job_id} has finished")
        else:
            print(f"Job {job_id}, has failed: aborting. Status: {job.status}")

        update_job_in_mss(mss_client, job_id=job_id, payload=job)


def postprocessing_failure_callback(
    _rq_job: rq.job.Job,
    _rq_connection: redis.Redis,
    _type: Type,
    value: Any,
    traceback: Any,
    **kwargs,
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
            jobs_store = Collection[Job](_rq_connection, schema=Job)
            job_id = value.job_id

            logging.error(value.exp)
            job = log_job_failure(
                jobs_store,
                job_id=job_id,
                reason="error during post processing",
            )
            update_job_in_mss(mss_client, job_id=job_id, payload=job)


def _is_job_shorter(storage_id: StorageID, duration: float) -> bool:
    """Checks if the job of given storage_id takes a shorter time than the duration

    Args:
        storage_id: the storage id for the given job in the expected format of {timestamp}-{uuid}-{duration}
        duration: the duration that the job should be shorter than

    Returns:
        True if the job's duration is shorter than the given duration, else False
    """
    return storage_id.duration < duration


def _get_idle_timer_id(booking_id: str) -> str:
    """Gets the rq job id for the idle timer job for the booking of given booking_id

    Args:
        booking_id: the unique identifier of the booking

    Returns:
        the rq job id for the idle timer job
    """
    return f"{booking_id}_idle_timer"


def _try_enqueue_on_normal_queue(
    job: Job,
    context: QueueContext,
) -> Tuple[str, QueueContext]:
    """Attempts to push the job to the normal queue, waitlisting it if it is too long

    If the job's execution duration is greater than the total time left on the execution queue before the next booking:
        - the job is placed on the waitlist
    Otherwise:
        - the job is pushed to the execution queue

    Args:
        job: the job that is to be run
        context: the context required when running a job on a queue

    Returns:
        the pair of the updated job's job ID and the context
    """
    from .queues import get_normal_execution_queue

    booking_db_url = context["booking_db_url"]
    queue_prefix = context["queue_prefix"]
    is_async = context["is_async"]
    job_id = job.job_id

    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)
    jobs_store = get_jobs_store(url=context["jobs_store_url"])

    next_booking = get_next_booking(bookings_sql_engine)

    connection = get_current_job().connection
    queue = get_normal_execution_queue(
        prefix=queue_prefix, connection=connection, is_async=is_async
    )

    if next_booking:
        time_to_next_booking = (next_booking.start_utc - get_utc_now()).total_seconds()
        queue_duration = queue.total_duration
        # usable time before the next booking starts must account for the jobs that are already scheduled
        usable_time = time_to_next_booking - queue_duration

        if job.estimated_duration > usable_time:
            _push_to_waitlist(job_id=job_id, context=context)
            return job_id, context

    job = update_job_stage(jobs_store, job_id=job_id, stage=Stage.EXEC_Q)
    queue.enqueue(job, context, job_id=get_rq_job_id(job_id, Stage.EXEC_Q))
    return job_id, context


def _try_enqueue_on_booked_queue(
    job: Job,
    context: QueueContext,
    booking_id: Optional[str] = None,
    is_from_waitlist: bool = False,
) -> Tuple[str, QueueContext]:
    """Attempts to push the job to the booked execution queue, failing it or waitlisting it if too long

    if force_normal_queue is True:
        - Push the job to the waitlist
    else if the user is owner of the current booking:
        - reset the booked_execution queue idle time counter
        - If the job's execution duration is greater than the total time left on the booked_execution queue:
            - raise error "job too long for the time left in the booking"
        - Otherwise:
            - Push the job to the booked_execution queue
    Otherwise if not from waitlist:
        - Push the job to the waitlist
    else:
        push it to the booked_execution queue

    Args:
        job: the job that is to be run
        context: the context required when running a job on a queue
        is_from_waitlist: whether the current job has been pushed from the waitlist; default = False
        booking_id: the unique identifier of the current booking if any

    Returns:
        the pair of the updated job's job ID and the context
    """
    from .queues import get_booked_execution_queue

    queue_prefix = context["queue_prefix"]
    is_async = context["is_async"]
    force_normal_queue = context.get("force_normal_queue")
    user_id = job.user_id
    booking_db_url = context["booking_db_url"]
    job_id = job.job_id

    job_store = get_jobs_store(url=context["jobs_store_url"])
    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)

    active_booking = None
    if booking_id:
        active_booking = get_booking(bookings_sql_engine, Booking.id == booking_id)

    connection = get_current_job().connection
    queue = get_booked_execution_queue(
        prefix=queue_prefix, connection=connection, is_async=is_async
    )

    usable_time = 0
    is_booker = False
    if isinstance(active_booking, Booking):
        usable_time = active_booking.total_duration - queue.total_duration
        is_booker = user_id == active_booking.user_id

    if is_booker:
        if force_normal_queue:
            _push_to_waitlist(job_id=job_id, context=context)
        elif job.estimated_duration > usable_time:
            job_store.update(
                job_id,
                {
                    "status": JobStatus.FAILED,
                    "failure_reason": "job too long for the time left in the booking",
                },
            )
        else:
            # restart the timer after this job is done if this is a job from the booker
            # Otherwise, if the booker sends no more jobs, this queue is taken over by other user's jobs
            reset_idleness_timer(
                end_utc=active_booking.end_utc,
                booking_id=active_booking.id,
                context=context,
                restart_in=job.estimated_duration,
            )
            job = update_job_stage(job_store, job_id=job_id, stage=Stage.EXEC_Q)
            queue.enqueue(job, context, job_id=get_rq_job_id(job_id, Stage.EXEC_Q))
        return job_id, context

    if not is_booker:
        if is_from_waitlist:
            if active_booking:
                # restart the timer to run once to push next waitlisted job to this queue
                # immediately after this job is done.
                # This will be canceled if a new job from the booker is sent to run on this queue
                reset_idleness_timer(
                    end_utc=active_booking.end_utc,
                    booking_id=active_booking.id,
                    context=context,
                    restart_in=job.estimated_duration,
                    max_idle_time=0,
                )
            job = update_job_stage(job_store, job_id=job_id, stage=Stage.EXEC_Q)
            queue.enqueue(job, context, job_id=get_rq_job_id(job_id, Stage.EXEC_Q))
        else:
            _push_to_waitlist(job_id=job_id, context=context)
        return job_id, context

    return job_id, context


def _push_to_waitlist(job_id: str, context: QueueContext) -> Tuple[str, QueueContext]:
    """Pushes a job to the waitlist

    Args:
        job_id: the id of the job
        context: the context that the job is to run in

    Returns:
        a tuple of the job_id and context
    """
    from .queues import get_waitlist

    connection = get_current_job().connection
    prefix = context["queue_prefix"]
    waitlist = get_waitlist(prefix=prefix, connection=connection)

    jobs_store = get_jobs_store(context["jobs_store_url"])
    uptodate_job = jobs_store.get_one(job_id)

    waitlist.add(uptodate_job)
    return job_id, context


def _pop_waitlist_to_booking(
    context: QueueContext,
) -> Tuple[Optional[str], QueueContext]:
    """Moves to the booked execution queue, the first waitlisted job that can finish before the next booking

    If there is no job to transfer, it returns no job and the context

    Args:
        context: extra variables that describe the environment the job is to run in

    Returns:
        a tuple of job_id/None and context
    """
    from .queues import get_waitlist

    booking_db_url = context["booking_db_url"]
    queue_prefix = context["queue_prefix"]
    redis_connection = get_current_job().connection

    waitlist = get_waitlist(prefix=queue_prefix, connection=redis_connection)
    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)

    usable_time = float("inf")
    next_booking = get_next_booking(bookings_sql_engine)
    if isinstance(next_booking, Booking):
        usable_time = (next_booking.start_utc - get_utc_now()).total_seconds()

    next_job = waitlist.pop_first(_is_job_shorter, usable_time)
    if next_job is None:
        return None, context

    return _try_enqueue_on_booked_queue(
        next_job, context, booking_id=None, is_from_waitlist=True
    )


def _pop_waitlist_to_normal_queue(context: QueueContext):
    """Moves to the normal execution queue, the waitlisted jobs that can finish before the next booking

    Args:
        context: extra variables that describe the environment the job is to run in

    Returns:
        a tuple of job_id/None and context
    """
    from .queues import (
        get_normal_execution_queue,
        get_waitlist,
    )

    booking_db_url = context["booking_db_url"]
    queue_prefix = context["queue_prefix"]
    is_async = context["is_async"]
    redis_connection = get_current_job().connection

    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)
    waitlist = get_waitlist(prefix=queue_prefix, connection=redis_connection)
    next_booking = get_next_booking(bookings_sql_engine)
    max_total_duration = None
    if next_booking:
        max_total_duration = (next_booking.start_utc - get_utc_now()).total_seconds()

    jobs = waitlist.pop_many(max_total_duration=max_total_duration)
    normal_queue = get_normal_execution_queue(
        prefix=queue_prefix, connection=redis_connection, is_async=is_async
    )
    for job in jobs:
        normal_queue.enqueue(job, context)
