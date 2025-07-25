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

from contextlib import suppress
from pathlib import Path
from typing import List

from fastapi import UploadFile
from pydantic import ValidationError
from rq import exceptions as rq_errors
from sqlmodel import or_

from ...libs.device_parameters import get_backend_config, get_device_calibration_info
from ...libs.queues.dtos import Job, JobStatus, Stage, StorageID
from ...utils.api import save_uploaded_file
from ...utils.datetime import get_utc_now, utc_now_str
from ...utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
    ItemNotFoundError,
    NotAuthenticatedError,
)
from ...utils.rq import cancel_rq_job
from ..booking import get_many_bookings
from ..booking.models import Booking, MSSTokenClaims, NewBookingInfo, User
from ..booking.service import (
    create_booking,
    delete_bookings,
    delete_users,
    get_active_booking,
    get_booking,
    get_user,
)
from ..booking.store import get_bookings_sql_engine
from .queues import QueuePool
from .store import get_jobs_store
from .tasks import post_booking_cleanup, reset_idleness_timer
from .utils import get_queue_context, get_rq_job_id


def submit_booking(
    queues: QueuePool, user_id: str, booking_info: NewBookingInfo
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
        queues: the collection of queues on which jobs run.
        booking_info: the details for the booking
        user_id: the ID of user submitting the booking

    Returns:
        the submitted booking
    """
    context = get_queue_context()
    booking_db_url = context["booking_db_url"]

    # create the new booking
    db_engine = get_bookings_sql_engine(url=booking_db_url)
    booking = create_booking(db_engine, user_id=user_id, data=booking_info)

    # at the start_utc, reset the idle time tracker
    queues.general.enqueue_at(
        booking.start_utc,
        reset_idleness_timer,
        booking_id=booking.id,
        end_utc=booking.end_utc,
        context=context,
        job_id=booking.start_event_id,
    )

    # at the end_utc of the booking, do proper cleanup of idleness timer,
    # waitlist and booked execution queues
    queues.general.enqueue_at(
        booking.end_utc,
        post_booking_cleanup,
        booking_id=booking.id,
        context=context,
        job_id=booking.end_event_id,
    )

    return booking


def cancel_booking(queues: QueuePool, user_id: str, booking_id: str):
    """Cancels the given booking as long as the user is the owner or is admin

    If the booking has already started, canceling fails.

    Args:
        queues: the collection of queues on which jobs run.
        booking_id: the unique identifier of the booking to cancel
        user_id: the ID of the user cancelling the booking

    Raises:
        ItemNotFoundError: the booking of id {booking_id} was not found
        ItemNotFoundError: the user of id {user_id} was not found
        BookingAlreadyActive: the booking of id {booking_id} is already active
        BookingAlreadyComplete: the booking of id {booking_id} is already completed.
    """
    context = get_queue_context()
    booking_db_url = context["booking_db_url"]

    db_engine = get_bookings_sql_engine(url=booking_db_url)
    user = get_user(db_engine, User.id == user_id)
    if user is None:
        raise ItemNotFoundError(f"the user of id {user_id} was not found")

    booking = get_booking(
        db_engine,
        Booking.id == booking_id,
        or_(Booking.user_id == user_id, user.is_admin == True),
    )
    if booking is None:
        raise ItemNotFoundError(f"the booking of id {booking_id} was not found")

    if booking.is_active:
        raise BookingAlreadyActiveError(
            f"the booking of id {booking_id} is already active"
        )

    if booking.is_complete:
        raise BookingAlreadyCompleteError(
            f"the booking of id {booking_id} is already complete"
        )

    _cancel_booking_from_queues(queues, booking=booking)
    delete_bookings(db_engine, Booking.id == booking_id)


def submit_job_file(
    queues: QueuePool,
    upload_file: UploadFile,
    upload_folder: Path,
    credentials: MSSTokenClaims,
    force_normal_queue: bool = False,
) -> Job:
    """Submits the job for processing

    On submission, the job is processed to determine how long it will take to execute.
    Then:
    In case there is a booking that is active at the current time:
        - if force_normal_queue is True:
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
        queues: the collection of queues that are to run the job.
        upload_file: the job file containing the job to submit for the next steps of processing
        upload_folder: the path to the folder where the job is to be uploaded
        credentials: MSS login details as got from the headers and the parameters or body
        force_normal_queue: the flag for whether to force the usage of the normal queue

    Returns:
        the submitted job
    """
    context = get_queue_context(force_normal_queue=force_normal_queue)
    jobs_store_url = context["jobs_store_url"]
    booking_db_url = context["booking_db_url"]

    # We save the job first in the jobs store before we put it on the queue
    # because it will be picked from the jobs store when the worker is running.
    # It would be harder to pass the job payload itself across each worker because it would have
    # to be pickled.
    store = get_jobs_store(url=jobs_store_url)

    job_id = credentials.job_id
    user_id = credentials.user_id
    if store.exists(job_id):
        raise ConflictError(f"job_id {job_id} already exists")

    # save job file
    new_file_path = upload_folder / job_id
    job_file_path = save_uploaded_file(upload_file, target=new_file_path)

    # save job in database
    backend_config = get_backend_config()
    calibration_info = get_device_calibration_info(backend_config)
    job = Job(
        job_id=job_id,
        device=backend_config.general_config.name,
        calibration_date=calibration_info.last_calibrated,
        user_id=user_id,
        stage=Stage.PRE_PROC_Q,
    )

    store.insert(job)

    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)
    active_booking = get_active_booking(db_engine=bookings_sql_engine)
    booking_id = None
    if active_booking:
        booking_id = active_booking.id

    queues.preprocessing.enqueue(
        job,
        context,
        booking_id=booking_id,
        job_file=job_file_path,
        job_id=get_rq_job_id(job_id, Stage.PRE_PROC_Q),
    )

    return job


def cancel_job(queues: QueuePool, job_id: str, user_id: str) -> Job:
    """Cancels the job of a given job_id if it belongs to the user or the user is admin

    Args:
        queues: the collection of queues on which jobs run.
        job_id: the unique identifier of the job
        user_id: the user_id of the user requesting the job

    Returns:
        the job

    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
        rq.exceptions.InvalidJobOperationError: if the job has already been cancelled
        rq.exceptions.InvalidJobOperation: if the job has already been cancelled
    """
    context = get_queue_context()
    booking_db_url = context["booking_db_url"]
    jobs_store_url = context["jobs_store_url"]

    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)
    user = get_user(bookings_sql_engine, User.id == user_id)

    if user is None:
        raise NotAuthenticatedError("user not found")

    job_store = get_jobs_store(url=jobs_store_url)
    job: Job = job_store.get_one(job_id)

    if not user.is_admin and job.user_id != user_id:
        raise ItemNotFoundError(f"Job {job_id} not found")

    _cancel_job_in_queues(queues, job)
    job = job_store.update(
        job_id,
        {
            "status": JobStatus.CANCELLED,
            "failure_reason": "Cancelled by a user",
            "updated_at": utc_now_str(),
        },
    )
    return job


def get_job(job_id: str, user_id: str) -> Job:
    """Get the job of a given job_id if it belongs to the user or the user is admin

    Args:
        job_id: the unique identifier of the job
        user_id: the user_id of the user requesting the job

    Returns:
        the job

    Raises:
        NotAuthenticatedError: user not found
        ItemNotFoundError: Job {job_id} not found
    """
    context = get_queue_context()
    booking_db_url = context["booking_db_url"]
    jobs_store_url = context["jobs_store_url"]

    bookings_sql_engine = get_bookings_sql_engine(url=booking_db_url)
    user = get_user(bookings_sql_engine, User.id == user_id)

    if user is None:
        raise NotAuthenticatedError("user not found")

    job_store = get_jobs_store(url=jobs_store_url)
    job: Job = job_store.get_one(job_id)

    if user.is_admin or job.user_id == user_id:
        return job

    raise ItemNotFoundError(f"Job {job_id} not found")


def delete_user_profile(queues: QueuePool, user_id: str):
    """Deletes the user profile for the given user_id

    On top of deleting the user, the user's active and pending bookings
    and active and pending jobs are canceled

    Args:
        queues: the collection of queues on which jobs run.
        user_id: the ID of the user whose profile is to be deleted

    Raises:
        ItemNotFoundError: the user of id {user_id} was not found
    """
    context = get_queue_context()
    booking_db_url = context["booking_db_url"]
    jobs_store_url = context["jobs_store_url"]

    db_engine = get_bookings_sql_engine(url=booking_db_url)
    user = get_user(db_engine, User.id == user_id)
    if user is None:
        raise ItemNotFoundError(f"the user of id {user_id} was not found")

    # cancel and delete jobs of the user
    job_store = get_jobs_store(url=jobs_store_url)
    user_pending_jobs: List[Job] = job_store.find_by_index(
        {"user_id": user_id, "status": JobStatus.PENDING}
    )

    job_update = {
        "status": JobStatus.CANCELLED,
        "failure_reason": "Cancelled on user deletion",
    }
    for job in user_pending_jobs:
        with suppress(
            rq_errors.InvalidJobOperation, rq_errors.InvalidJobOperationError
        ):
            _cancel_job_in_queues(queues, job)
        # update cancelled status of the job
        job_store.update(job.job_id, job_update)

    # cancel bookings of the user
    user_incomplete_bookings = get_many_bookings(
        db_engine, Booking.end_utc > get_utc_now(), Booking.user_id == user_id
    )
    booking_ids = []
    for booking in user_incomplete_bookings:
        booking_ids.append(booking.id)
        _cancel_booking_from_queues(queues, booking=booking)

    # delete the bookings in the database
    delete_bookings(db_engine, Booking.id.in_(booking_ids))

    # delete the user
    delete_users(db_engine, User.id == user_id)


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
    with suppress(rq_errors.NoSuchJobError):
        queues.preprocessing.cancel_job(
            job_id=job.job_id,
            storage_id=preprocessing_storage_id,
            ignore_errors=ignore_errors,
        )
    with suppress(rq_errors.NoSuchJobError):
        queues.normal_execution.cancel_job(
            job_id=job.job_id,
            storage_id=execution_storage_id,
            ignore_errors=ignore_errors,
        )
    with suppress(rq_errors.NoSuchJobError):
        queues.booked_execution.cancel_job(
            job_id=job.job_id,
            storage_id=execution_storage_id,
            ignore_errors=ignore_errors,
        )

    with suppress(rq_errors.NoSuchJobError):
        queues.postprocessing.cancel_job(
            job_id=job.job_id,
            storage_id=execution_storage_id,
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
