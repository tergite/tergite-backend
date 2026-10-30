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
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import UploadFile
from sqlmodel import or_

from ...libs.queues.dtos import Job, JobStatus, Stage
from ...utils.datetime import get_utc_now, utc_now_str
from ...utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
    ItemNotFoundError,
    JobAlreadyCompleteError,
    NotAuthenticatedError,
)
from ...utils.messages import GeneralMessage
from ..booking import (
    Booking,
    BookingsConfig,
    MSSTokenClaims,
    NewBookingInfo,
    User,
    create_booking,
    delete_bookings,
    delete_users,
    get_booking,
    get_many_bookings,
    get_user,
)
from ..devices import get_device_calibration_info
from . import dispatcher
from .dtos import RecalibrationInfo
from .resources import DeviceResources


def submit_booking(
    device: DeviceResources,
    user_id: str,
    booking_info: NewBookingInfo,
) -> Booking:
    """Submits a booking, whose start and end the dispatcher compares with now

    Args:
        device: what the device runs on
        booking_info: the details for the booking
        user_id: the ID of user submitting the booking

    Returns:
        the submitted booking
    """
    return create_booking(
        device.bookings_db,
        user_id=user_id,
        data=booking_info,
        config=BookingsConfig.from_settings(device.settings),
        now=device.settings.get_utc_now(),
    )


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

    delete_bookings(device.bookings_db, Booking.id == booking_id)


def submit_job_file(
    device: DeviceResources,
    upload_file: UploadFile,
    credentials: MSSTokenClaims,
    should_force_normal_queue: bool = False,
) -> Job:
    """Submits the job, which the dispatcher preprocesses, then executes as the bookings let it

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

    job_id = credentials.job_id
    user_id = credentials.user_id
    if device.jobs_store.is_stored(job_id):
        raise ConflictError(f"job_id {job_id} already exists")

    # save job file
    new_file_path = upload_folder / job_id
    _save_uploaded_file(upload_file, target=new_file_path)

    # save job in database
    calibration_info = get_device_calibration_info(device.redis, backend_name)
    job = Job(
        job_id=job_id,
        device=backend_name,
        calibration_date=calibration_info.last_calibrated,
        user_id=user_id,
        stage=Stage.PRE_PROC_Q,
    )

    dispatcher.enqueue_job(device, job, is_booking_excluded=should_force_normal_queue)
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

    device.jobs_store.delete(job_id)
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
    return device.jobs_store.find(
        user_id=user_id or None, status=status or None, skip=skip, limit=limit
    )


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
    user_jobs = job_store.find(user_id=user_id)
    for job in user_jobs:
        if job.status.is_terminal():
            continue
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
    booking_ids = [booking.id for booking in user_incomplete_bookings]

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
    """
    if interval is None:
        interval = device.settings.default_recalibration_interval
    dispatcher.schedule_recalibration(device, due_at=start_timestamp, interval=interval)
    return {"status": "success"}


def stop_recalibration(device: DeviceResources) -> None:
    """Cancels the device's scheduled recalibrations; one under way schedules no next

    Args:
        device: what the device runs on
    """
    dispatcher.stop_recalibration(device)


def get_recalibration_info(device: DeviceResources) -> RecalibrationInfo:
    """Gets the basic information about recalibration

    Args:
        device: what the device runs on

    Returns:
        the basic information about the recalibration
    """
    return dispatcher.get_recalibration_info(device)


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
