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
from rq import Repeat

from app.libs.qiskit.qobj import PulseQobj

from ...libs.device_parameters import DeviceCalibration
from ...libs.qiskit_providers.utils import json_decoder
from ...libs.quantum_executor.base.quantum_job import (
    MeasLvl,
    discriminate_results,
    read_job_from_hdf5,
    xarray_to_list,
)
from ...libs.queues.dtos import (
    Job,
    JobStatus,
    Stage,
    StorageID,
)
from ...utils.datetime import get_utc_now
from ...utils.exc import JobAlreadyCancelled, PostProcessingError
from ...utils.rq import cancel_rq_job
from ..booking import (
    Booking,
    get_active_booking,
    get_booking,
    get_next_booking,
)
from ..devices import get_device_calibration_info
from ..external.mss import DeviceEvent, DeviceEventName
from .dtos import JobFile
from .resources import DeviceResources
from .utils import (
    apply_linear_discriminator,
    decompress_qobj,
    get_rq_job_id,
    log_job_failure,
    move_file,
    update_job_in_mss,
    update_job_results,
    update_job_stage,
)


def preprocess(
    device: DeviceResources,
    job: Job,
    job_file: Path,
    booking_id: Optional[str] = None,
    should_force_normal_queue: bool = False,
) -> str:
    """Prepares the job for execution

    One of the most important thing it does is to set the estimated_duration of the job.
    This requires compiling the job to get the absolute timings of the schedules.

    if should_force_normal_queue:
        - Push the job to the waitlist

    Args:
        device: what the device runs on
        job: the job that is to be preprocessed
        job_file: the path to the job file
        booking_id: the unique identifier of the current booking if any
        should_force_normal_queue: whether a booker's job is to wait for the normal queue

    Returns:
        the updated job's job ID
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
            executor = device.executor
            duration, _ = executor.preprocess(
                PulseQobj.from_dict(qobj),
                job_id=job_id,
                results_folder=results_folder,
            )
        job = jobs_store.update(job.job_id, {"estimated_duration": duration})

        if booking_id is None:
            return _try_enqueue_on_normal_queue(device, job)
        else:
            return _try_enqueue_on_booked_queue(
                device,
                job,
                booking_id=booking_id,
                should_force_normal_queue=should_force_normal_queue,
            )
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


def post_booking_cleanup(device: DeviceResources, booking_id: str):
    """Cleans up after the booking of the given booking_id

    It should cancel the idle time interval job and allow for all the jobs on the booked_execution queues to clear out.
    It should also move all jobs that are short enough on the waitlist to the execution queue,
    even when the booking has been deleted, as with its user.

    Args:
        device: what the device runs on
        booking_id: the unique identifier of the booking
    """
    booking = get_booking(device.bookings_db, Booking.id == booking_id)

    # a deleted booking's idleness timer was cancelled when it was deleted
    if booking is not None:
        cancel_rq_job(device.redis, job_id=booking.idle_timer_id)

    # push to normal execution queue all jobs that have duration
    # small enough to run before next booking
    _pop_waitlist_to_normal_queue(device)


def reset_idleness_timer(
    device: DeviceResources,
    booking_id: str,
    end_utc: datetime,
    restart_in: float = 0,
    max_idle_time: Optional[float] = None,
):
    """Refreshes the idle timer for the booking of given booking_id

    If end_utc is in the past, no, new timer will be created but the old one will be removed

    Args:
        device: what the device runs on
        booking_id: the unique identifier of the booking whose idleness timer is to be reset
        end_utc: the timestamp beyond which the timer should not run
        restart_in: the number of seconds when the timer should restart; default = 0 i.e. immediately
        max_idle_time: the maximum acceptable time for queue to be idle

    Returns:
        the refreshed rq.job.Job that is constructed and enqueued to track the time or None if end_utc is in the past
    """
    if max_idle_time is None:
        max_idle_time = device.settings.max_idle_time

    general_queue = device.queues.general

    booking = get_booking(device.bookings_db, Booking.id == booking_id)
    if booking is None:
        logging.error(f"no booking found with id {booking_id}")
        return None

    cancel_rq_job(device.redis, job_id=booking.idle_timer_id)

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
        # rq specific kwargs
        job_id=booking.idle_timer_id,
        repeat=repeat,
    )
    return None


def execute(device: DeviceResources, job: Job) -> Tuple[str, str]:
    """Runs the job given the file containing compiled experiments

    Args:
        device: what the device runs on
        job: the job that is to be run

    Returns:
        the pair of the updated job's job ID and the results file path
    """
    job_id = job.job_id
    jobs_store = device.jobs_store
    preprocessing_dir = device.settings.preprocessed_job_pool

    try:
        update_job_stage(jobs_store, job_id=job_id, stage=Stage.EXEC_W)

        # Just a locking mechanism to ensure jobs don't interfere with each other
        with device.executor_lock:
            executor = device.executor
            results_file = executor.run(job_id, inputs_folder=preprocessing_dir)

        job: Job = jobs_store.get_one(job_id)
        if job.status == JobStatus.CANCELLED:
            raise JobAlreadyCancelled("cancelled")

        postproc_queue = device.queues.postprocessing
        job = update_job_stage(jobs_store, job_id=job_id, stage=Stage.PST_PROC_Q)
        postproc_queue.enqueue(
            job,
            results_file=results_file,
            job_id=get_rq_job_id(job_id, Stage.PST_PROC_Q),
        )

        # clean up
        logging.info("Job executed successfully")
        return job.job_id, results_file
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
                device.redis, backend_name=backend_name
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


def postprocessing_success_callback(
    rq_job: rq.job.Job, _rq_connection, job_id: str, *args, **kwargs
):
    """Callback to invoke when postprocessing succeeds

    Args:
        rq_job: the rq job, which the worker gave its device's resources
        _rq_connection: the redis connection
        job_id: the result of the postprocessing, the job's ID
    """
    device: DeviceResources = rq_job.device_resources
    jobs_store = device.jobs_store

    job = update_job_stage(jobs_store, job_id=job_id, stage=Stage.FINAL_Q)
    if job.status == JobStatus.SUCCESSFUL:
        job = update_job_stage(jobs_store, job_id=job_id, stage=Stage.FINAL_W)

    update_job_in_mss(
        device.mss_client,
        payload=job,
        job_supervisor_log=device.settings.job_supervisor_log,
    )


def postprocessing_failure_callback(
    rq_job: rq.job.Job,
    _rq_connection: redis.Redis,
    _type: Type,
    value: Any,
    traceback: Any,
    **kwargs,
):
    """Callback to be called when postprocessing fails

    Args:
        rq_job: the rq job, which the worker gave its device's resources
        _rq_connection: the redis connection
        _type: the error type
        value: the value passed to the callback from the handler
        traceback: the error traceback
    """
    if isinstance(value, PostProcessingError):
        logging.error(value.exp)
        _fail_job(
            rq_job.device_resources,
            job_id=value.job_id,
            reason="error during post processing",
        )


def recalibrate(
    device: DeviceResources, interval: Optional[float] = None
) -> Optional[rq.job.Job]:
    """Recalibrates the device, having first scheduled the next recalibration

    The next is scheduled first so that a failure here does not end the repeats, and a
    cancellation while this one runs cancels the next.

    Args:
        device: what the device runs on
        interval: the interval in seconds between two concurrent jobs; default = None i.e. no repeat

    Returns:
        the rq.job.Job that is constructed and enqueued to schedule next recalibration, or None if interval was not set
    """
    next_job = _schedule_next_recalibration(device, interval=interval)

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

    return next_job


def _is_job_shorter(storage_id: StorageID, duration: float) -> bool:
    """Checks if the job of given storage_id takes a shorter time than the duration

    Args:
        storage_id: the storage id for the given job in the expected format of {timestamp}-{uuid}-{duration}
        duration: the duration that the job should be shorter than

    Returns:
        True if the job's duration is shorter than the given duration, else False
    """
    return storage_id.duration < duration


def _try_enqueue_on_normal_queue(device: DeviceResources, job: Job) -> str:
    """Attempts to push the job to the normal queue, waitlisting it if it is too long

    If the job's execution duration is greater than the total time left on the execution queue before the next booking:
        - the job is placed on the waitlist
    Otherwise:
        - the job is pushed to the execution queue

    Args:
        device: what the device runs on
        job: the job that is to be run

    Returns:
        the updated job's job ID
    """
    job_id = job.job_id
    jobs_store = device.jobs_store

    next_booking = get_next_booking(device.bookings_db)
    queue = device.queues.normal_execution

    if next_booking:
        time_to_next_booking = (next_booking.start_utc - get_utc_now()).total_seconds()
        queue_duration = queue.total_duration
        # usable time before the next booking starts must account for the jobs that are already scheduled
        usable_time = time_to_next_booking - queue_duration

        if job.estimated_duration > usable_time:
            _push_to_waitlist(device, job_id=job_id)
            return job_id

    job = update_job_stage(jobs_store, job_id=job_id, stage=Stage.EXEC_Q)
    queue.enqueue(job, job_id=get_rq_job_id(job_id, Stage.EXEC_Q))
    return job_id


def _try_enqueue_on_booked_queue(
    device: DeviceResources,
    job: Job,
    booking_id: str,
    should_force_normal_queue: bool,
) -> str:
    """Attempts to push the job to the booked execution queue, failing it or waitlisting it if too long

    if the booking has ended, or been deleted, since the job was submitted:
        - Try the normal queue instead
    else if the user is owner of the current booking:
        - If should_force_normal_queue:
            - Push the job to the waitlist
        - If the job's execution duration is greater than the total time left on the booked_execution queue:
            - raise error "job too long for the time left in the booking"
        - Otherwise:
            - reset the booked_execution queue idle time counter
            - Push the job to the booked_execution queue
    Otherwise:
        - Push the job to the waitlist

    Args:
        device: what the device runs on
        job: the job that is to be run
        booking_id: the unique identifier of the booking under way when the job was submitted
        should_force_normal_queue: whether a booker's job is to wait for the normal queue

    Returns:
        the updated job's job ID
    """
    user_id = job.user_id
    job_id = job.job_id

    active_booking = get_booking(device.bookings_db, Booking.id == booking_id)
    # its end has drained the waitlist already, so nothing would release the job from it
    if active_booking is None or active_booking.is_complete_at(
        device.settings.get_utc_now()
    ):
        return _try_enqueue_on_normal_queue(device, job)

    queue = device.queues.booked_execution

    time_left = (active_booking.end_utc - get_utc_now()).total_seconds()
    usable_time = time_left - queue.total_duration
    is_booker = user_id == active_booking.user_id

    if is_booker:
        if should_force_normal_queue:
            _push_to_waitlist(device, job_id=job_id)
        elif job.estimated_duration > usable_time:
            _fail_job(
                device,
                job_id=job_id,
                reason="job too long for the time left in the booking",
            )
        else:
            # restart the timer after this job is done if this is a job from the booker
            # Otherwise, if the booker sends no more jobs, this queue is taken over by other user's jobs
            reset_idleness_timer(
                device,
                end_utc=active_booking.end_utc,
                booking_id=active_booking.id,
                restart_in=job.estimated_duration,
            )
            job = update_job_stage(device.jobs_store, job_id=job_id, stage=Stage.EXEC_Q)
            queue.enqueue(job, job_id=get_rq_job_id(job_id, Stage.EXEC_Q))
        return job_id

    _push_to_waitlist(device, job_id=job_id)
    return job_id


def _push_to_waitlist(device: DeviceResources, job_id: str) -> str:
    """Pushes a job to the waitlist

    Args:
        device: what the device runs on
        job_id: the id of the job

    Returns:
        the job_id
    """
    uptodate_job = device.jobs_store.get_one(job_id)
    device.queues.waitlist.add(uptodate_job, job_id=get_rq_job_id(job_id, Stage.EXEC_Q))
    return job_id


def _pop_waitlist_to_booking(device: DeviceResources) -> Optional[str]:
    """Moves to the booked execution queue, the first waitlisted job that can finish before the next booking

    Args:
        device: what the device runs on

    Returns:
        the job_id of the job moved, or None if there was none to move
    """
    waitlist = device.queues.waitlist
    queue = device.queues.booked_execution

    usable_time = float("inf")
    next_booking = get_next_booking(device.bookings_db)
    if isinstance(next_booking, Booking):
        usable_time = (next_booking.start_utc - get_utc_now()).total_seconds()

    next_job_tuple = waitlist.pop_first(_is_job_shorter, usable_time)
    if next_job_tuple is None:
        return None

    job, args, kwargs = next_job_tuple
    active_booking = get_active_booking(device.bookings_db)
    if active_booking:
        # restart the timer to run once to push next waitlisted job to this queue
        # immediately after this job is done.
        # This will be canceled if a new job from the booker is sent to run on this queue
        reset_idleness_timer(
            device,
            end_utc=active_booking.end_utc,
            booking_id=active_booking.id,
            restart_in=job.estimated_duration,
            max_idle_time=0,
        )
    job = update_job_stage(device.jobs_store, job_id=job.job_id, stage=Stage.EXEC_Q)
    queue.enqueue(job, *args, **kwargs)

    return job.job_id


def _pop_waitlist_to_normal_queue(device: DeviceResources):
    """Moves to the normal execution queue, the waitlisted jobs that can finish before the next booking

    Args:
        device: what the device runs on
    """
    next_booking = get_next_booking(device.bookings_db)
    max_total_duration = None
    if next_booking is not None:
        max_total_duration = (next_booking.start_utc - get_utc_now()).total_seconds()

    job_tuples = device.queues.waitlist.pop_many(max_total_duration=max_total_duration)
    normal_queue = device.queues.normal_execution

    for job, args, kwargs in job_tuples:
        job = update_job_stage(device.jobs_store, job_id=job.job_id, stage=Stage.EXEC_Q)
        normal_queue.enqueue(job, *args, **kwargs)


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


def _schedule_next_recalibration(
    device: DeviceResources, interval: Optional[float]
) -> Optional[rq.job.Job]:
    """Schedules a recalibration interval seconds from now, if interval is set"""
    if not isinstance(interval, float):
        return None

    func_name = f"{__name__}.{recalibrate.__qualname__}"
    job = device.queues.recalibration.enqueue_in(
        timedelta(seconds=interval),
        func_name,
        interval=interval,
    )

    # save some metadata on the job for later retrieval
    job.meta["interval"] = interval
    job.save_meta()
    return job
