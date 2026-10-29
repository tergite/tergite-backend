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
"""Stores the backlog that the performance baselines list: finished jobs and future bookings
of PERF_BACKLOG_USER_ID, on the device that BCC_ENV_FILE names; dev/perf/record.sh runs it
"""
import os
from datetime import timedelta

from sqlmodel import Session

import settings
from app.libs.queues.dtos import Job, JobStatus, Stage
from app.services.booking import Booking, User, get_bookings_sql_engine
from app.services.scheduler import get_jobs_store
from app.utils.datetime import get_utc_now, utc_now_str
from app.utils.redis import get_redis_connection
from app.utils.strings import uuid_str

JOB_COUNT = 500
BOOKING_COUNT = 200


def main() -> None:
    user_id = os.environ["PERF_BACKLOG_USER_ID"]
    store_jobs(user_id)
    store_bookings(user_id)


def store_jobs(user_id: str) -> None:
    jobs_store = get_jobs_store(
        get_redis_connection(settings.RQ_REDIS_URL), settings.get_device_settings()
    )
    for _ in range(JOB_COUNT):
        jobs_store.insert(
            Job(
                device=settings.DEFAULT_PREFIX,
                calibration_date=utc_now_str(),
                user_id=user_id,
                estimated_duration=0.001,
                stage=Stage.FINAL_W,
                status=JobStatus.SUCCESSFUL,
            )
        )


def store_bookings(user_id: str) -> None:
    """Stores hourly bookings from tomorrow on, so that none is under way during the run"""
    first_start = get_utc_now() + timedelta(days=1)
    with Session(get_bookings_sql_engine(settings.BOOKING_DB_URL)) as session:
        session.add(User(id=user_id, name="Perf", email="perf@example.com"))
        for index in range(BOOKING_COUNT):
            start_utc = first_start + timedelta(hours=index)
            session.add(
                Booking.model_validate(
                    {
                        "id": uuid_str(),
                        "user_id": user_id,
                        "start_utc": start_utc,
                        "end_utc": start_utc + timedelta(minutes=30),
                    }
                )
            )
        session.commit()


if __name__ == "__main__":
    main()
