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
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from fastapi import UploadFile
from sqlmodel import or_

from libs.quantum_executor.base.circuits import QasmCircuit
from libs.quantum_executor.base.device_checks import check_circuit_fits_device
from libs.quantum_executor.base.gates import NativeCircuit, PulseBlock
from libs.quantum_executor.base.pulse_limits import (
    check_pulse_limits,
    get_calibrated_frame_frequencies,
)
from libs.quantum_executor.base.qasm import to_native_circuit
from libs.queues.dtos import Job, JobStatus, Stage
from utils.datetime import get_utc_now, utc_now_str
from utils.exc import (
    BookingAlreadyActiveError,
    BookingAlreadyCompleteError,
    ConflictError,
    InvalidCircuitError,
    InvalidRequestError,
    ItemNotFoundError,
    JobAlreadyCompleteError,
    NotAuthenticatedError,
    UnauthorizedError,
    UnsupportedCircuitError,
)
from utils.messages import GeneralMessage

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
from .dtos import JobFile, RecalibrationInfo
from .resources import DeviceResources
from .utils import delete_job_files, update_job_in_mss
from .wakeups import wake_workers


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
    booking = create_booking(
        device.bookings_db,
        device.settings.default_prefix,
        user_id=user_id,
        booking_info=booking_info,
        config=BookingsConfig.from_settings(device.settings),
        now=device.settings.get_utc_now(),
    )
    wake_workers(device, "heavy")
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
        device.settings.default_prefix,
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

    delete_bookings(
        device.bookings_db, device.settings.default_prefix, Booking.id == booking_id
    )
    wake_workers(device, "heavy")


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

    Raises:
        ConflictError: job_id {job_id} already exists
        InvalidCircuitError: a circuit the device cannot run, such as one that is not
            valid OpenQASM 3 or needs a coupler the device lacks; see
            qasm.to_native_circuit and device_checks.check_circuit_fits_device
        UnauthorizedError: the job's project may not send pulse-level jobs
        InvalidRequestError: a pulse-level block beyond the device's limits; see
            pulse_limits.check_pulse_limits
    """
    job_id = credentials.job_id
    if device.jobs_store.is_stored(job_id):
        raise ConflictError(f"job_id {job_id} already exists")

    _check_circuits(device, upload_file, credentials)
    _save_uploaded_file(upload_file, target=device.settings.job_upload_pool / job_id)
    job = _get_new_job(device, credentials)
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
    get_job(device, job_id=job_id, user_id=user_id, is_mss_admin=is_mss_admin)
    if reason is None:
        reason = "Cancelled by a user"

    cancelled_job = device.jobs_store.mark_ended(
        job_id,
        {
            "status": JobStatus.CANCELLED,
            "cancellation_reason": reason,
            "updated_at": utc_now_str(),
        },
    )
    # a job cancelled with its job token, not through MSS, is news to MSS
    update_job_in_mss(device.bookings_db, device.mss_client, payload=cancelled_job)
    return cancelled_job


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
    with suppress(JobAlreadyCompleteError):
        job = cancel_job(
            device, job_id=job_id, user_id=user_id, is_mss_admin=is_mss_admin
        )

    device.jobs_store.delete(job_id)
    delete_job_files(device.settings, job_id)
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

    _cancel_pending_jobs(device, user_id)
    _cancel_incomplete_bookings(device, user_id)
    delete_users(device.bookings_db, User.id == user_id)
    wake_workers(device, "heavy")


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


def _check_circuits(
    device: DeviceResources, upload_file: UploadFile, credentials: MSSTokenClaims
) -> None:
    """Refuses a job whose circuits do not translate into native gates or do not fit the
    device's qubits and couplers, or a pulse-level job its project may not send or beyond
    the device's limits; what the calibration holds, as which qubits are calibrated, is
    left to the job's preprocessing, as the device may be recalibrated before it runs

    Raises:
        InvalidCircuitError: a circuit that does not translate or fit, with the reason
        UnauthorizedError: the job's project may not send pulse-level jobs
        InvalidRequestError: a pulse-level block beyond the device's limits
    """
    upload_file.file.seek(0)
    job_file = JobFile.model_validate_json(upload_file.file.read())
    upload_file.file.seek(0)
    backend_config = device.executor_options.backend_config
    dt = backend_config.general_config.dt
    device_config = backend_config.device_config
    circuits = [
        _get_fitting_circuit(
            circuit,
            index=index,
            dt=dt,
            max_clbits=backend_config.general_config.max_clbits,
            qubit_ids=device_config.qubit_numbers,
            couplings=[pair for pair, _ in device_config.qubit_ids_coupler_map],
        )
        for index, circuit in enumerate(job_file.params.circuits)
    ]
    if not any(isinstance(g, PulseBlock) for c in circuits for g in c.gates):
        return
    if not credentials.is_pulse_allowed:
        raise UnauthorizedError("the job's project may not send pulse-level jobs")
    try:
        check_pulse_limits(
            circuits,
            shots=job_file.params.shots,
            limits=backend_config.pulse_limits,
            frame_frequencies=get_calibrated_frame_frequencies(backend_config),
            dt=dt,
        )
    except UnsupportedCircuitError as exp:
        raise InvalidRequestError(f"{exp}") from exp


def _get_fitting_circuit(
    circuit: QasmCircuit,
    index: int,
    dt: float,
    max_clbits: int,
    qubit_ids: Sequence[int],
    couplings: Iterable[Tuple[int, int]],
) -> NativeCircuit:
    """Translates a job file's circuit into native gates that fit the device's qubits and
    couplers

    Args:
        circuit: the circuit, as uploaded
        index: the circuit's index in the job file's circuits
        dt: the device's sample time in seconds
        max_clbits: the most classical bits the device takes in a circuit
        qubit_ids: the numbers in the device's qubits' ids, in order
        couplings: the pairs of qubits the device's couplers couple, by the numbers in
            their ids

    Returns:
        the circuit of native gates

    Raises:
        InvalidCircuitError: the circuit does not translate or fit, with the reason and
            index
    """
    try:
        native_circuit = to_native_circuit(
            circuit, dt=dt, max_qubits=len(qubit_ids), max_clbits=max_clbits
        )
        check_circuit_fits_device(native_circuit, qubit_ids, couplings=couplings)
        return native_circuit
    except UnsupportedCircuitError as exp:
        raise InvalidCircuitError(
            f"{exp}", circuit_index=index, line=exp.line, column=exp.column
        ) from exp


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


def _get_new_job(device: DeviceResources, credentials: MSSTokenClaims) -> Job:
    """Gets the job of the credentials, at the stage before preprocessing, on the device's
    current calibration"""
    backend_name = device.executor_options.backend_name
    calibration_info = get_device_calibration_info(device.bookings_db, backend_name)
    return Job(
        job_id=credentials.job_id,
        device=backend_name,
        calibration_date=calibration_info.last_calibrated,
        user_id=credentials.user_id,
        stage=Stage.PRE_PROC_Q,
    )


def _cancel_pending_jobs(device: DeviceResources, user_id: str) -> None:
    """Cancels the user's jobs that have not ended, queued or running"""
    for job in device.jobs_store.find(user_id=user_id):
        if job.status.is_terminal():
            continue
        # it may end after it was found
        with suppress(JobAlreadyCompleteError):
            device.jobs_store.mark_ended(
                job.job_id,
                {
                    "status": JobStatus.CANCELLED,
                    "cancellation_reason": "Cancelled on user deletion",
                    "updated_at": utc_now_str(),
                },
            )


def _cancel_incomplete_bookings(device: DeviceResources, user_id: str) -> None:
    """Deletes the user's bookings that have not ended"""
    device_name = device.settings.default_prefix
    incomplete_bookings = get_many_bookings(
        device.bookings_db,
        device_name,
        Booking.end_utc > get_utc_now(),
        Booking.user_id == user_id,
    )
    delete_bookings(
        device.bookings_db,
        device_name,
        Booking.id.in_([booking.id for booking in incomplete_bookings]),
    )
