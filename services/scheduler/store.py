# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""The jobs of a device, in its SQL database"""

from datetime import datetime, timedelta
from typing import List, Optional, Self

from sqlalchemy import Engine
from sqlmodel import Field, Session, SQLModel, col, delete, select

from libs.queues.dtos import Job, JobStatus
from utils.config import DeviceSettings
from utils.datetime import get_utc_now, to_utc
from utils.exc import ItemNotFoundError
from utils.sql_db import open_immediate_session
from utils.strings import uuid_str


class JobRow(SQLModel, table=True):
    """A job, in the jobs table that the devices sharing a database share"""

    __tablename__ = "job"

    device_name: str = Field(primary_key=True)
    job_id: str = Field(primary_key=True)
    user_id: Optional[str] = Field(default=None, index=True)
    status: str = Field(index=True)
    # when the store last wrote it, which its expiry counts from
    updated_at: datetime = Field(index=True)
    # the job, as JSON
    data: str
    stage: str = Field(default="", index=True)
    estimated_duration: Optional[float] = None
    created_at: Optional[datetime] = Field(default=None, index=True)
    # the dispatcher's: when a worker claimed the job's current stage, when its execution
    # ended, whether, booked, it waits for the time outside bookings, and its results
    claimed_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    is_booking_excluded: bool = False
    results_file: Optional[str] = None


class RecalibrationRow(SQLModel, table=True):
    """A recalibration of a device, done, under way or due, as the dispatcher keeps them"""

    __tablename__ = "recalibration"

    id: str = Field(default_factory=uuid_str, primary_key=True)
    device_name: str = Field(index=True)
    status: str = Field(index=True)
    due_at: datetime = Field(index=True)
    interval: Optional[float] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    error: Optional[str] = None


class JobsStore:
    """The jobs of one device, which expire `ttl` seconds after they were last written"""

    def __init__(self, db_engine: Engine, device_name: str, ttl: float):
        self._db_engine = db_engine
        self._device_name = device_name
        self._ttl = timedelta(seconds=ttl)

    @classmethod
    def from_settings(cls, db_engine: Engine, device_settings: DeviceSettings) -> Self:
        """Gets the store of the device of these settings, in the given database

        Args:
            db_engine: the engine of the database the jobs are kept in
            device_settings: the settings of the device

        Returns:
            the device's jobs store
        """
        return cls(
            db_engine,
            device_name=device_settings.default_prefix,
            ttl=device_settings.jobs_store_ttl,
        )

    def is_stored(self, job_id: str) -> bool:
        """Checks whether the device has the job

        Args:
            job_id: the id of the job

        Returns:
            True if the job is stored
        """
        with Session(self._db_engine) as session:
            return session.get(JobRow, (self._device_name, job_id)) is not None

    def insert(self, job: Job) -> Job:
        """Inserts the job, replacing any of the same id, and deletes the expired ones

        Args:
            job: the job to insert

        Returns:
            the inserted job
        """
        now = get_utc_now()
        with open_immediate_session(self._db_engine) as session:
            session.exec(
                delete(JobRow).where(
                    col(JobRow.device_name) == self._device_name,
                    col(JobRow.updated_at) < now - self._ttl,
                )
            )
            row = JobRow(device_name=self._device_name, job_id=job.job_id, data="")
            _write_job(row, job, now=now)
            session.merge(row)
            session.commit()
        return job

    def get_one(self, job_id: str) -> Job:
        """Gets the job of the given id

        Args:
            job_id: the id of the job

        Returns:
            the job

        Raises:
            ItemNotFoundError: '{job_id}' not found
        """
        with Session(self._db_engine) as session:
            row = session.get(JobRow, (self._device_name, job_id))
            if row is None:
                raise ItemNotFoundError(f"'{job_id}' not found")
            return Job.model_validate_json(row.data)

    def update(self, job_id: str, updates: dict) -> Job:
        """Sets the given fields of the job, and returns it

        Args:
            job_id: the id of the job
            updates: the new values of the fields, by name

        Returns:
            the updated job

        Raises:
            ItemNotFoundError: '{job_id}' not found
        """
        # immediate, so that no other process writes the job between the read and the write
        with open_immediate_session(self._db_engine) as session:
            row = session.get(JobRow, (self._device_name, job_id))
            if row is None:
                raise ItemNotFoundError(f"'{job_id}' not found")
            job = Job.model_validate_json(row.data)
            updated_job = Job.model_validate(job.model_dump() | updates)
            _write_job(row, updated_job, now=get_utc_now())
            session.add(row)
            session.commit()
        return updated_job

    def delete(self, job_id: str) -> None:
        """Deletes the job of the given id, if the device has it

        Args:
            job_id: the id of the job
        """
        with Session(self._db_engine) as session:
            session.exec(
                delete(JobRow).where(
                    col(JobRow.device_name) == self._device_name,
                    col(JobRow.job_id) == job_id,
                )
            )
            session.commit()

    def find(
        self,
        user_id: Optional[str] = None,
        status: Optional[JobStatus] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> List[Job]:
        """Finds the jobs of the user, or of the status, or all, least recently written first

        Args:
            user_id: the id of the user whose jobs to find; None for any user's
            status: the status of the jobs to find; None for any status
            skip: the number of jobs to skip
            limit: the most jobs to return; None for no limit

        Returns:
            the jobs found
        """
        query = select(JobRow).where(col(JobRow.device_name) == self._device_name)
        if user_id is not None:
            query = query.where(col(JobRow.user_id) == user_id)
        if status is not None:
            query = query.where(col(JobRow.status) == status.value)
        query = query.order_by(col(JobRow.updated_at)).offset(skip).limit(limit)
        with Session(self._db_engine) as session:
            return [Job.model_validate_json(row.data) for row in session.exec(query)]


def _write_job(row: JobRow, job: Job, now: datetime) -> None:
    """Writes the job into its row, leaving the dispatcher's columns as they are"""
    row.user_id = job.user_id
    row.status = job.status.value
    row.updated_at = now
    row.data = job.model_dump_json()
    row.stage = job.stage.name
    row.estimated_duration = job.estimated_duration
    row.created_at = (
        to_utc(datetime.fromisoformat(job.created_at)) if job.created_at else now
    )
