# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Runs the jobs as rows of the SQL database, which each worker claims, instead of on RQ's queues

A device's heavy worker claims the job the bookings let execute, then its oldest job to
preprocess, then its due recalibration; a light worker claims every device's oldest job to
postprocess. A booking's start and end are times the claim compares with now, a recalibration
is a row with a due time, a cancelled job one no claim takes, and a job whose stage outlives
its timeout is failed by whichever worker next looks.
"""
import logging
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, NoReturn, Optional

from sqlalchemy import Engine
from sqlmodel import Session, col, delete, or_, select
from sqlmodel.sql.expression import SelectOfScalar

from ...libs.queues.dtos import Job, JobStatus, Stage
from ...utils.datetime import get_utc_now, to_utc
from ...utils.sql_db import open_immediate_session
from ..booking import Booking, get_active_booking, get_next_booking
from ..devices import is_offline
from .dtos import RecalibrationInfo
from .resources import DeviceResources
from .store import JobRow, RecalibrationRow
from .utils import log_job_failure, update_job_in_mss, update_job_stage

# a worker's pause, in seconds, when it found nothing to claim
_IDLE_PAUSE_SECONDS = 0.2
# picks, in the session, a waiting job of the device at the stage, or none
_RowPicker = Callable[[Session, DeviceResources, Stage], Optional[JobRow]]
_TERMINAL_STATUSES = tuple(status.value for status in JobStatus if status.is_terminal())


class Dispatcher:
    """Claims and runs the work of its devices: the heavy work, a device's recalibrations,
    preprocessing and executions; the light work, every device's postprocessing"""

    # a heavy worker runs one device, which switching off stops
    should_wait_while_offline = True

    def __init__(
        self,
        devices: dict[str, DeviceResources],
        should_run_heavy: bool,
        should_run_light: bool,
    ):
        self.device_resources = devices
        self._should_run_heavy = should_run_heavy
        self._should_run_light = should_run_light
        self._is_stopped = False

    def run(self, is_burst: bool = False, max_jobs: Optional[int] = None) -> bool:
        """Runs what is due until stopped, or, in a burst, until nothing is

        Args:
            is_burst: whether to stop once nothing is due
            max_jobs: the most stages or recalibrations to run; default: no limit

        Returns:
            whether it ran anything
        """
        run_count = 0
        while not self._is_stopped and (max_jobs is None or run_count < max_jobs):
            has_run = False
            for device in self.device_resources.values():
                if self._run_once(device):
                    has_run = True
                    run_count += 1
                    break
            if not has_run:
                if is_burst:
                    break
                time.sleep(_IDLE_PAUSE_SECONDS)
        return run_count > 0

    def stop(self) -> None:
        """Stops the run after the work under way"""
        self._is_stopped = True

    def _run_once(self, device: DeviceResources) -> bool:
        fail_overdue_jobs(device)
        if self._should_run_light and run_next_postprocessing(device):
            return True
        if not self._should_run_heavy:
            return False

        device_name = device.settings.default_prefix
        if self.should_wait_while_offline and is_offline(device.redis, device_name):
            return False
        # recalibration last, when the device has no job to run
        return (
            run_next_execution(device)
            or run_next_preprocessing(device)
            or run_due_recalibration(device)
        )


def enqueue_job(
    device: DeviceResources, job: Job, is_booking_excluded: bool = False
) -> None:
    """Stores the new job for a heavy worker to preprocess

    Args:
        device: what the device runs on
        job: the job, at its preprocessing queue's stage
        is_booking_excluded: whether a booker's job is to wait for the time outside bookings
    """
    device.jobs_store.insert(job)
    if is_booking_excluded:
        with Session(device.bookings_db) as session:
            row = session.get(JobRow, (device.settings.default_prefix, job.job_id))
            row.is_booking_excluded = True
            session.add(row)
            session.commit()


def mark_preprocessed(device: DeviceResources, job: Job) -> str:
    """Leaves the preprocessed job for the bookings to let execute, or fails a booker's that
    is too long for what is left of their booking

    Returns:
        the job's id
    """
    now = get_utc_now()
    booking = get_active_booking(device.bookings_db)
    if (
        booking is not None
        and booking.user_id == job.user_id
        and not _is_booking_excluded(device, job.job_id)
        and (job.estimated_duration or 0) > (booking.end_utc - now).total_seconds()
    ):
        _fail_job(
            device, job.job_id, reason="job too long for the time left in the booking"
        )
        return job.job_id

    update_job_stage(device.jobs_store, job_id=job.job_id, stage=Stage.EXEC_Q)
    return job.job_id


def mark_executed(device: DeviceResources, job_id: str, results_file: str) -> Job:
    """Leaves the executed job for a light worker to postprocess its results file"""
    job = update_job_stage(device.jobs_store, job_id=job_id, stage=Stage.PST_PROC_Q)
    with Session(device.bookings_db) as session:
        row = session.get(JobRow, (device.settings.default_prefix, job_id))
        row.executed_at = get_utc_now()
        row.results_file = results_file
        session.add(row)
        session.commit()
    return job


def run_next_preprocessing(device: DeviceResources) -> bool:
    """Preprocesses the device's oldest job waiting for it

    Returns:
        whether there was one
    """
    from .tasks import preprocess

    row = _claim_job(device, Stage.PRE_PROC_Q, _pick_oldest)
    if row is None:
        return False
    job = Job.model_validate_json(row.data)
    job_file = device.settings.job_upload_pool / job.job_id
    _run_logged(
        preprocess,
        device,
        job,
        job_file=job_file,
        timeout=device.settings.max_preprocessing_time,
    )
    return True


def run_next_execution(device: DeviceResources) -> bool:
    """Executes the job the bookings let run next on the device

    Returns:
        whether there was one
    """
    from .tasks import execute

    row = _claim_job(device, Stage.EXEC_Q, _pick_next_execution)
    if row is None:
        return False
    job = Job.model_validate_json(row.data)
    _run_logged(execute, device, job, timeout=device.settings.max_execution_time)
    return True


def run_next_postprocessing(device: DeviceResources) -> bool:
    """Postprocesses the device's oldest executed job

    Returns:
        whether there was one
    """
    from .tasks import fail_postprocessing, mark_postprocessed, postprocess

    row = _claim_job(device, Stage.PST_PROC_Q, _pick_oldest)
    if row is None:
        return False
    job = Job.model_validate_json(row.data)
    try:
        with _raise_after(device.settings.max_postprocessing_time):
            postprocess(device, job, results_file=row.results_file)
    except Exception as exp:
        fail_postprocessing(device, exp)
    else:
        mark_postprocessed(device, job.job_id)
    return True


def run_due_recalibration(device: DeviceResources) -> bool:
    """Recalibrates the device if one is due, having first scheduled the next

    Returns:
        whether one was due
    """
    from .tasks import recalibrate_now

    now = get_utc_now()
    with open_immediate_session(device.bookings_db) as session:
        row = session.exec(
            select(RecalibrationRow)
            .where(
                col(RecalibrationRow.device_name) == device.settings.default_prefix,
                col(RecalibrationRow.status) == "scheduled",
                col(RecalibrationRow.due_at) <= now,
            )
            .order_by(col(RecalibrationRow.due_at))
            .with_for_update(skip_locked=True)
        ).first()
        if row is None:
            return False
        row.status, row.started_at = "started", now
        session.add(row)
        if row.interval:
            session.add(
                RecalibrationRow(
                    device_name=row.device_name,
                    status="scheduled",
                    due_at=now + timedelta(seconds=row.interval),
                    interval=row.interval,
                )
            )
        session.commit()
        recalibration_id = row.id

    status, error = "finished", None
    try:
        with _raise_after(device.settings.max_recalibration_queue_time):
            recalibrate_now(device)
    except Exception as exp:
        logging.error(f"recalibration failed: {exp}")
        status, error = "failed", f"{exp!r}"
    _record_recalibration_end(
        device.bookings_db, recalibration_id, status=status, error=error
    )
    return True


def fail_overdue_jobs(device: DeviceResources) -> None:
    """Fails the device's jobs whose stage has run past its timeout, as their worker died"""
    timeouts = {
        Stage.PRE_PROC_W: device.settings.max_preprocessing_time,
        Stage.EXEC_W: device.settings.max_execution_time,
        Stage.PST_PROC_W: device.settings.max_postprocessing_time,
    }
    now = get_utc_now()
    with Session(device.bookings_db) as session:
        overdue_job_ids = session.exec(
            select(JobRow.job_id).where(
                col(JobRow.device_name) == device.settings.default_prefix,
                col(JobRow.status).not_in(_TERMINAL_STATUSES),
                or_(
                    *(
                        (col(JobRow.stage) == stage.name)
                        & (col(JobRow.claimed_at) < now - timedelta(seconds=timeout))
                        for stage, timeout in timeouts.items()
                    )
                ),
            )
        ).all()
    for job_id in overdue_job_ids:
        _fail_job(device, job_id, reason="timed out")


def schedule_recalibration(
    device: DeviceResources, due_at: Optional[datetime], interval: float
) -> None:
    """Schedules the device's recalibration, repeating every interval, in place of any other"""
    stop_recalibration(device)
    with Session(device.bookings_db) as session:
        session.add(
            RecalibrationRow(
                device_name=device.settings.default_prefix,
                status="scheduled",
                due_at=to_utc(due_at) if due_at else get_utc_now(),
                interval=interval,
            )
        )
        session.commit()


def stop_recalibration(device: DeviceResources) -> None:
    """Cancels the device's scheduled recalibrations; one under way schedules no next"""
    with Session(device.bookings_db) as session:
        session.exec(
            delete(RecalibrationRow).where(
                col(RecalibrationRow.device_name) == device.settings.default_prefix,
                col(RecalibrationRow.status) == "scheduled",
            )
        )
        for row in session.exec(
            select(RecalibrationRow).where(
                col(RecalibrationRow.device_name) == device.settings.default_prefix,
                col(RecalibrationRow.status) == "started",
            )
        ):
            row.interval = None
            session.add(row)
        session.commit()


def get_recalibration_info(device: DeviceResources) -> RecalibrationInfo:
    """Gets the device's recalibration under way, the last ended and the next due"""
    device_name = device.settings.default_prefix
    with Session(device.bookings_db) as session:
        query = select(RecalibrationRow).where(
            col(RecalibrationRow.device_name) == device_name
        )
        current = session.exec(
            query.where(col(RecalibrationRow.status) == "started")
        ).first()
        previous = session.exec(
            query.where(
                col(RecalibrationRow.status).in_(("finished", "failed"))
            ).order_by(col(RecalibrationRow.ended_at).desc())
        ).first()
        upcoming = session.exec(
            query.where(col(RecalibrationRow.status) == "scheduled").order_by(
                col(RecalibrationRow.due_at)
            )
        ).first()

    info = {}
    if previous:
        info.update(
            previous_job_id=previous.id,
            previous_job_status=previous.status,
            previous_job_started_at=_to_aware(previous.started_at),
            previous_job_ended_at=_to_aware(previous.ended_at),
            previous_job_error=previous.error,
            interval=previous.interval,
        )
    if current:
        info.update(
            current_job_id=current.id,
            current_job_status=current.status,
            current_job_started_at=_to_aware(current.started_at),
            interval=current.interval,
        )
    if upcoming:
        info.update(
            next_job_id=upcoming.id,
            next_job_status=upcoming.status,
            next_job_enqueued_at=_to_aware(upcoming.due_at),
            interval=upcoming.interval,
        )
    return RecalibrationInfo(**info)


def get_next_change_at(device: DeviceResources) -> Optional[datetime]:
    """Gets when the bookings next change which waiting job may execute, if one waits"""
    with Session(device.bookings_db) as session:
        has_waiting_job = (
            session.exec(_get_waiting_query(device, Stage.EXEC_Q)).first() is not None
        )
    if not has_waiting_job:
        return None

    now = get_utc_now()
    times = []
    booking = get_active_booking(device.bookings_db)
    if booking is not None:
        times.append(booking.end_utc)
        times.append(
            _get_booker_active_at(device, booking) + _get_max_idle_time(device)
        )
    next_booking = get_next_booking(device.bookings_db)
    if next_booking is not None:
        times.append(next_booking.start_utc)
    later_times = [value for value in times if value > now]
    return min(later_times, default=None)


def _claim_job(
    device: DeviceResources, stage: Stage, select_row: _RowPicker
) -> Optional[JobRow]:
    """Claims the job select_row picks among those waiting at the stage, moving it on to the
    stage's worker, in one transaction no other worker's claim can interleave"""
    with open_immediate_session(device.bookings_db) as session:
        row = select_row(session, device, stage)
        if row is None:
            return None
        row.stage = Stage(stage + 1).name
        row.claimed_at = get_utc_now()
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _pick_oldest(
    session: Session, device: DeviceResources, stage: Stage
) -> Optional[JobRow]:
    return session.exec(
        _get_waiting_query(device, stage).with_for_update(skip_locked=True)
    ).first()


def _pick_next_execution(
    session: Session, device: DeviceResources, stage: Stage
) -> Optional[JobRow]:
    """Selects the booker's oldest job that fits what is left of their booking, or, once they
    are idle, or outside bookings, the oldest of anyone's that ends before the next booking
    """
    now = get_utc_now()
    waiting = _get_waiting_query(device, stage).with_for_update(skip_locked=True)
    next_booking = get_next_booking(device.bookings_db)
    seconds_to_next = (
        (next_booking.start_utc - now).total_seconds() if next_booking else None
    )

    booking = get_active_booking(device.bookings_db)
    if booking is not None:
        seconds_left = (booking.end_utc - now).total_seconds()
        booker_job = session.exec(
            waiting.where(
                col(JobRow.user_id) == booking.user_id,
                col(JobRow.is_booking_excluded).is_(False),
                col(JobRow.estimated_duration) <= seconds_left,
            )
        ).first()
        if booker_job is not None:
            return booker_job
        if now - _get_booker_active_at(device, booking) < _get_max_idle_time(device):
            return None

    if seconds_to_next is not None:
        waiting = waiting.where(col(JobRow.estimated_duration) <= seconds_to_next)
    return session.exec(waiting).first()


def _get_waiting_query(device: DeviceResources, stage: Stage) -> SelectOfScalar[JobRow]:
    return (
        select(JobRow)
        .where(
            col(JobRow.device_name) == device.settings.default_prefix,
            col(JobRow.stage) == stage.name,
            col(JobRow.status).not_in(_TERMINAL_STATUSES),
        )
        .order_by(col(JobRow.created_at))
    )


def _get_booker_active_at(device: DeviceResources, booking: Booking) -> datetime:
    """Gets when the booker last had a job executing, or else when their booking started"""
    with Session(device.bookings_db) as session:
        rows = session.exec(
            select(JobRow).where(
                col(JobRow.device_name) == device.settings.default_prefix,
                col(JobRow.user_id) == booking.user_id,
                or_(
                    col(JobRow.stage) == Stage.EXEC_W.name,
                    col(JobRow.executed_at) >= booking.start_utc,
                ),
            )
        ).all()
    if any(row.stage == Stage.EXEC_W.name for row in rows):
        return get_utc_now()
    executed_times = [_to_aware(row.executed_at) for row in rows if row.executed_at]
    return max([booking.start_utc, *executed_times])


def _get_max_idle_time(device: DeviceResources) -> timedelta:
    return timedelta(seconds=device.settings.max_idle_time)


def _is_booking_excluded(device: DeviceResources, job_id: str) -> bool:
    with Session(device.bookings_db) as session:
        row = session.get(JobRow, (device.settings.default_prefix, job_id))
        return row is not None and row.is_booking_excluded


def _record_recalibration_end(
    db_engine: Engine, recalibration_id: str, status: str, error: Optional[str]
) -> None:
    with Session(db_engine) as session:
        row = session.get(RecalibrationRow, recalibration_id)
        if row is None:
            return
        row.status, row.ended_at, row.error = status, get_utc_now(), error
        session.add(row)
        session.commit()


def _fail_job(device: DeviceResources, job_id: str, reason: str) -> None:
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


def _run_logged(
    task: Callable[..., Any],
    device: DeviceResources,
    *args: Any,
    timeout: float,
    **kwargs: Any,
) -> None:
    """Runs the task, which fails its job itself, logging what it raised"""
    try:
        with _raise_after(timeout):
            task(device, *args, **kwargs)
    except TimeoutError:
        job: Job = args[0]
        _fail_job(device, job.job_id, reason="timed out")
    except Exception as exp:
        logging.error(f"{task.__name__} failed: {exp!r}")


@contextmanager
def _raise_after(seconds: float) -> Iterator[None]:
    """Raises TimeoutError in the block once it has run the seconds, from the main thread only,
    which alone receives signals"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def raise_timeout(*_: Any) -> NoReturn:
        raise TimeoutError(f"took longer than {seconds} seconds")

    previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _to_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Gets the UTC datetime that SQLite gave back without its timezone"""
    return to_utc(value) if value is not None else None
