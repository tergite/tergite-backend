# This code is part of Tergite
#
# (C) Nicklas Botö, Fabian Forslund 2022
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
# Modified:
#
# - Martin Ahindura, 2023, 2025
#
"""Module containing service for scheduling jobs"""
import logging
import shutil
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import UploadFile
from pydantic import ValidationError
from rq import exceptions as rq_errors
from sqlmodel import or_

from ...libs.queues.dtos import Job, JobStatus, Stage, StorageID
from ...utils.datetime import get_utc_now, to_utc, utc_now_str
from ...utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
    ItemNotFoundError,
    JobAlreadyCancelled,
    JobAlreadyCompleteError,
    NotAuthenticatedError,
)
from ...utils.messages import GeneralMessage
from ...utils.rq import cancel_rq_job
from ..booking import (
    Booking,
    BookingsConfig,
    MSSTokenClaims,
    NewBookingInfo,
    User,
    create_booking,
    delete_bookings,
    delete_users,
    get_active_booking,
    get_booking,
    get_many_bookings,
    get_user,
)
from ..devices import get_device_calibration_info
from .dtos import RecalibrationInfo
from .queues import QueuePool
from .resources import DeviceResources
from .utils import get_rq_job_id

# the tasks go by import path, as importing them loads the executor's heavy libraries
_TASKS_MODULE = f"{__package__}.tasks"


def submit_booking(
    device: DeviceResources,
    user_id: str,
    booking_info: NewBookingInfo,
) -> Booking:
    """Submits a booking for registration

    When a booking that is active:
        - the idle time of the queue is checked at a given interval
            - if the idle time of the booked_execution queue has exceeded MAX_SLOT_JOB_INTERVAL
                - Find the first job on the waitlist that has a duration that is less than
                  the total time left on the execution queue before the next booking:
                    - push that job to booked_execution queue

    When a booking ends:
        - wait for all jobs on the booked_execution queue to run to completion
        - clear the idle time timer
        - Repeatedly find the first job on the waitlist that has a duration that is less than
          the total time left on the execution queue before the next booking until the waitlist is empty:
            - push that job to execution queue

    On submission of a new booking, new events are scheduled to run, one at the `start_utc`
    and the other at the `end_utc` of this booking to enforce the above conditions.

    Args:
        device: what the device runs on
        booking_info: the details for the booking
        user_id: the ID of user submitting the booking

    Returns:
        the submitted booking
    """
    # create the new booking
    booking = create_booking(
        device.bookings_db,
        user_id=user_id,
        data=booking_info,
        config=BookingsConfig.from_settings(device.settings),
        now=device.settings.get_utc_now(),
    )

    # at the start_utc, reset the idle time tracker
    device.queues.general.enqueue_at(
        booking.start_utc,
        f"{_TASKS_MODULE}.reset_idleness_timer",
        booking_id=booking.id,
        end_utc=booking.end_utc,
        job_id=booking.start_event_id,
    )

    # at the end_utc of the booking, do proper cleanup of idleness timer,
    # waitlist and booked execution queues
    device.queues.general.enqueue_at(
        booking.end_utc,
        f"{_TASKS_MODULE}.post_booking_cleanup",
        booking_id=booking.id,
        job_id=booking.end_event_id,
    )

    return booking


def cancel_booking(
    device: DeviceResources,
    user_id: str,
    booking_id: str,
    is_mss_admin: bool = False,
):
    """Cancels the given booking as long as the user is the owner or is admin

    If the booking has already started, canceling fails.

    Args:
        device: what the device runs on
        booking_id: the unique identifier of the booking to cancel
        user_id: the ID of the user cancelling the booking
        is_mss_admin: whether the user is an MSS admin

    Raises:
        ItemNotFoundError: the booking of id {booking_id} was not found
        ItemNotFoundError: the user of id {user_id} was not found
        BookingAlreadyActive: the booking of id {booking_id} is already active
        BookingAlreadyComplete: the booking of id {booking_id} is already completed.
    """
    user = get_user(device.bookings_db, User.id == user_id)
    if user is None and not is_mss_admin:
        raise ItemNotFoundError(f"the user of id {user_id} was not found")

    # MSS knows its admins before they have used this device
    is_admin = is_mss_admin or user.is_admin
    booking = get_booking(
        device.bookings_db,
        Booking.id == booking_id,
        or_(Booking.user_id == user_id, is_admin == True),
    )
    if booking is None:
        raise ItemNotFoundError(f"the booking of id {booking_id} was not found")

    now = device.settings.get_utc_now()
    if booking.is_active_at(now):
        raise BookingAlreadyActiveError(
            f"the booking of id {booking_id} is already active"
        )

    if booking.is_complete_at(now):
        raise BookingAlreadyCompleteError(
            f"the booking of id {booking_id} is already complete"
        )

    _cancel_booking_from_queues(device.queues, booking=booking)
    delete_bookings(device.bookings_db, Booking.id == booking_id)


def submit_job_file(
    device: DeviceResources,
    upload_file: UploadFile,
    credentials: MSSTokenClaims,
    should_force_normal_queue: bool = False,
) -> Job:
    """Submits the job for processing

    On submission, the job is processed to determine how long it will take to execute.
    Then:
    In case there is a booking that is active at the current time:
        - if should_force_normal_queue:
            - Push the job to the waitlist
        - else if the user is owner of the current booking:
            - reset the booked_execution queue idle time counter
            - If the job's execution duration is greater than the total time left on the booked_execution queue:
                - raise error "job too long for the time left in the booking"
            - Otherwise:
                - Push the job to the booked_execution queue
        - Otherwise:
            - Push the job to the waitlist

    In case there is no active booking:
        - If the job's execution duration is greater than the total time left on the execution queue before the next booking:
            - the job is placed on the waitlist
        - Otherwise:
            - the job is pushed to the execution queue

    Args:
        device: what the device runs on
        upload_file: the job file containing the job to submit for the next steps of processing
        credentials: MSS login details as got from the headers and the parameters or body
        should_force_normal_queue: whether a booker's job is to wait for the normal queue

    Returns:
        the submitted job
    """
    upload_folder = device.settings.job_upload_pool
    backend_name = device.executor_options.backend_name

    # We save the job first in the jobs store before we put it on the queue
    # because it will be picked from the jobs store when the worker is running.
    # It would be harder to pass the job payload itself across each worker because it would have
    # to be pickled.
    jobs_store = device.jobs_store

    job_id = credentials.job_id
    user_id = credentials.user_id
    if jobs_store.exists(job_id):
        raise ConflictError(f"job_id {job_id} already exists")

    # save job file
    new_file_path = upload_folder / job_id
    job_file_path = _save_uploaded_file(upload_file, target=new_file_path)

    # save job in database
    calibration_info = get_device_calibration_info(device.redis, backend_name)
    job = Job(
        job_id=job_id,
        device=backend_name,
        calibration_date=calibration_info.last_calibrated,
        user_id=user_id,
        stage=Stage.PRE_PROC_Q,
    )

    jobs_store.insert(job)

    active_booking = get_active_booking(db_engine=device.bookings_db)
    booking_id = None
    if active_booking:
        booking_id = active_booking.id

    device.queues.preprocessing.enqueue(
        job,
        booking_id=booking_id,
        job_file=job_file_path,
        should_force_normal_queue=should_force_normal_queue,
        job_id=get_rq_job_id(job_id, Stage.PRE_PROC_Q),
    )

    return job


def cancel_job(
    device: DeviceResources,
    job_id: str,
    user_id: str,
    is_mss_admin: bool = False,
    reason: Optional[str] = None,
) -> Job:
    """Cancels the job of a given job_id if it belongs to the user or the user is admin

    Args:
        device: what the device runs on
        job_id: the unique identifier of the job
        user_id: the user_id of the user requesting the job
        is_mss_admin: whether the user is an MSS admin
        reason: the justification for the cancellation

    Returns:
        the job

    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
        JobAlreadyCompleteError: job {job_id} is already {status}
    """
    job = get_job(device, job_id=job_id, user_id=user_id, is_mss_admin=is_mss_admin)
    if job.status.is_terminal():
        raise JobAlreadyCompleteError(f"job {job_id} is already {job.status.value}")

    _cancel_job_in_queues(device.queues, job)
    if reason is None:
        reason = "Cancelled by a user"

    return device.jobs_store.update(
        job_id,
        {
            "status": JobStatus.CANCELLED,
            "cancellation_reason": reason,
            "updated_at": utc_now_str(),
        },
    )


def delete_job(
    device: DeviceResources,
    job_id: str,
    user_id: str,
    is_mss_admin: bool = False,
) -> Job:
    """Deletes the job of a given job_id if it belongs to the user or the user is admin

    Args:
        device: what the device runs on
        job_id: the unique identifier of the job
        user_id: the user_id of the user requesting the job
        is_mss_admin: whether the user is an MSS admin

    Returns:
        the job

    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
    """
    job = get_job(device, job_id=job_id, user_id=user_id, is_mss_admin=is_mss_admin)
    if not job.status.is_terminal():
        job = cancel_job(
            device, job_id=job_id, user_id=user_id, is_mss_admin=is_mss_admin
        )

    job_store = device.jobs_store

    try:
        job_store.delete_many((job_id,))
    except ValidationError as exp:
        logging.error(f"error deleting job {job_id}: {exp}")
        raise ItemNotFoundError(f"Job {job_id} not found")

    return job


def get_job(
    device: DeviceResources,
    job_id: str,
    user_id: str,
    is_mss_admin: bool = False,
) -> Job:
    """Get the job of a given job_id if it belongs to the user or the user is admin

    Args:
        device: what the device runs on
        job_id: the unique identifier of the job
        user_id: the user_id of the user requesting the job
        is_mss_admin: whether the user is an MSS admin

    Returns:
        the job

    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
    """
    user = get_user(device.bookings_db, User.id == user_id)
    if user is None and not is_mss_admin:
        raise NotAuthenticatedError("user not found")

    job: Job = device.jobs_store.get_one(job_id)

    # MSS knows its admins before they have used this device
    is_admin = is_mss_admin or user.is_admin
    is_owner = job.user_id == user_id
    if is_admin or is_owner:
        return job

    raise ItemNotFoundError(f"Job {job_id} not found")


def get_many_jobs(
    device: DeviceResources,
    user_id: Optional[str] = None,
    status: Optional[JobStatus] = None,
    skip: int = 0,
    limit: int | None = None,
) -> List[Job]:
    """Get the job of a given job_id if it belongs to the user or the user is admin

    Args:
        device: what the device runs on
        user_id: the user_id associated with the jobs; defaults to None i.e. all jobs are returned
        status: the status of the jobs; defaults to None i.e. all jobs are returned
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.

    Returns:
        the list of job
    """
    job_store = device.jobs_store
    filters = {}
    if status:
        filters["status"] = status
    if user_id:
        filters["user_id"] = user_id

    return job_store.find_by_index(filters, skip=skip, limit=limit)


def delete_user_profile(device: DeviceResources, user_id: str) -> None:
    """Deletes the user profile for the given user_id

    On top of deleting the user, the user's active and pending bookings
    and active and pending jobs are canceled

    Args:
        device: what the device runs on
        user_id: the ID of the user whose profile is to be deleted

    Raises:
        ItemNotFoundError: the user of id {user_id} was not found
    """
    user = get_user(device.bookings_db, User.id == user_id)
    if user is None:
        raise ItemNotFoundError(f"the user of id {user_id} was not found")

    # cancel the user's jobs that have not finished, queued or running
    job_store = device.jobs_store
    user_jobs: List[Job] = job_store.find_by_index({"user_id": user_id})
    for job in user_jobs:
        if job.status.is_terminal():
            continue
        with suppress(
            rq_errors.InvalidJobOperation, rq_errors.InvalidJobOperationError
        ):
            _cancel_job_in_queues(device.queues, job)
        job_store.update(
            job.job_id,
            {
                "status": JobStatus.CANCELLED,
                "cancellation_reason": "Cancelled on user deletion",
                "updated_at": utc_now_str(),
            },
        )

    # cancel bookings of the user
    user_incomplete_bookings = get_many_bookings(
        device.bookings_db, Booking.end_utc > get_utc_now(), Booking.user_id == user_id
    )
    booking_ids = []
    for booking in user_incomplete_bookings:
        booking_ids.append(booking.id)
        _cancel_booking_from_queues(device.queues, booking=booking)
        if booking.is_active_at(device.settings.get_utc_now()):
            # its end, now cancelled, would have released the waitlist
            device.queues.general.enqueue(
                f"{_TASKS_MODULE}.post_booking_cleanup",
                booking_id=booking.id,
            )

    # delete the bookings in the database
    delete_bookings(device.bookings_db, Booking.id.in_(booking_ids))

    # delete the user
    delete_users(device.bookings_db, User.id == user_id)


def init_recalibration(
    device: DeviceResources,
    start_timestamp: Optional[datetime] = None,
    interval: Optional[float] = None,
) -> GeneralMessage:
    """Initializes recalibration of the device at the given time

    Args:
        device: what the device runs on
        start_timestamp: the timestamp when recalibration should start; defaults to None meaning as soon as possible.
        interval: the interval in seconds between two recalibrations; defaults to None,
            meaning use the default recalibration interval.

    Returns:
        the general message indicating status of request

    Raises:
        IsOfflineError: the scheduler is offline
    """
    # just make sure no other recalibration is running or scheduled before starting again
    stop_recalibration(device, ignore_errors=True)

    if interval is None:
        interval = device.settings.default_recalibration_interval

    if isinstance(start_timestamp, datetime):
        job = device.queues.recalibration.enqueue_at(
            # rq reads a naive datetime as the server's local time; the API means UTC
            datetime=to_utc(start_timestamp),
            f=f"{_TASKS_MODULE}.recalibrate",
            interval=interval,
        )
    else:
        job = device.queues.recalibration.enqueue(
            f=f"{_TASKS_MODULE}.recalibrate",
            interval=interval,
        )

    # save some metadata on the job for later retrieval
    job.meta["interval"] = interval
    job.save_meta()

    return {"status": "success"}


def stop_recalibration(device: DeviceResources, ignore_errors: bool = False):
    """Cancels recalibration if scheduled or queued

    Args:
        device: what the device runs on
        ignore_errors: whether to ignore errors during cancellation of recalibration; defaults to False

    Raises:
        ItemNotFoundError: if the job does not exist
        JobAlreadyCancelled: if the job is already cancelled
        IsOfflineError: the scheduler is offline
    """
    pending_job_ids = device.queues.recalibration.get_job_ids()
    pending_job_ids += device.queues.recalibration.scheduled_job_registry.get_job_ids()

    for job_id in pending_job_ids:
        try:
            cancel_rq_job(device.redis, job_id=job_id, ignore_errors=ignore_errors)
        except rq_errors.NoSuchJobError:
            raise ItemNotFoundError("recalibration job does not exist")
        except (rq_errors.InvalidJobOperationError, rq_errors.InvalidJobOperation):
            raise JobAlreadyCancelled("recalibration job is already cancelled")


def get_recalibration_info(device: DeviceResources) -> RecalibrationInfo:
    """Gets the basic information about recalibration

    Args:
        device: what the device runs on

    Returns:
        the basic information about the recalibration

    Raises:
        RuntimeError: multiple current jobs found: [...]
    """
    return RecalibrationInfo.from_queue(device.queues.recalibration)


def _cancel_job_in_queues(queues: QueuePool, job: Job, ignore_errors: bool = False):
    """Cancels the given job in the queues

    Args:
        queues: the collection of queue for this application
        job: the job which is to be cancelled
        ignore_errors: whether errors should be silently ignored or not

    Raises:
        rq.exceptions.InvalidJobOperationError: if the job has already been cancelled
        rq.exceptions.InvalidJobOperation: if the job has already been cancelled
    """
    job_id = job.job_id
    preprocessing_storage_id = StorageID.from_details(uuid=job_id, duration=None)
    execution_storage_id = StorageID.from_details(
        uuid=job_id, duration=job.estimated_duration
    )

    with suppress(ValidationError):
        queues.waitlist.pop_job(execution_storage_id)
    queues.preprocessing.remove_job(preprocessing_storage_id)
    queues.normal_execution.remove_job(execution_storage_id)
    queues.booked_execution.remove_job(execution_storage_id)
    queues.postprocessing.remove_job(execution_storage_id)

    # rq job ids are global, so the normal and booked execution queues share the EXEC_Q one
    connection = queues.preprocessing.connection
    with suppress(rq_errors.NoSuchJobError):
        cancel_rq_job(
            connection,
            get_rq_job_id(job_id, Stage.PRE_PROC_Q),
            ignore_errors=ignore_errors,
        )
    with suppress(rq_errors.NoSuchJobError):
        cancel_rq_job(
            connection, get_rq_job_id(job_id, Stage.EXEC_Q), ignore_errors=ignore_errors
        )
    with suppress(rq_errors.NoSuchJobError):
        cancel_rq_job(
            connection,
            get_rq_job_id(job_id, Stage.PST_PROC_Q),
            ignore_errors=ignore_errors,
        )


def _cancel_booking_from_queues(queues: QueuePool, booking: Booking):
    """Cancels the booking from the queues

    Args:
        queues: the collection of queues that handle the bookings
        booking: the booking whose trace is to be removed
    """
    queue_connection = queues.general.connection
    # cancel and remove any idleness timers
    cancel_rq_job(queue_connection, booking.idle_timer_id, ignore_errors=True)

    # cancel and remove any start_event jobs
    cancel_rq_job(queue_connection, booking.start_event_id, ignore_errors=True)

    # cancel and remove any end_event jobs
    cancel_rq_job(queue_connection, booking.end_event_id, ignore_errors=True)


def _save_uploaded_file(file: UploadFile, target: Path) -> Path:
    """Saves the uploaded file to the given target path

    Args:
        file: the file to upload
        target: the target path to save to

    Returns:
        the new path to the saved file
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    file.file.seek(0)
    with target.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)
    file.file.close()

    return target
