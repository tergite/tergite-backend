# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2023
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""A worker file that is preloaded with the heavy libraries etc"""
import json
import logging
import os
import sys

from rq import Worker
from rq.job import Job as RqJob
from rq.queue import Queue as RqQueue


class PreloadedRqWorker(Worker):
    """RQ Worker that has important libraries, connections etc. preloaded"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # initialize connections and other globals,
        # so that they are available to forked child processes
        from app.libs.queues.dtos import get_queue_context
        from app.services.external.mss.service import get_mss_client
        from app.services.scheduler.queues import get_queue_pool
        from app.services.scheduler.store import get_jobs_store
        from app.services.scheduler.utils import get_quantum_executor
        from app.utils.redis import get_redis_connection

        _executor = get_quantum_executor()
        _redis_connection = get_redis_connection()
        _mss_client = get_mss_client()
        _queue_context = get_queue_context()
        _queue_pool = get_queue_pool()
        _jobs_store = get_jobs_store()

    def execute_job(self, job: RqJob, queue: RqQueue):
        """Override to pre-cache booking state in Redis before forking.

        SQLite is not usable inside forked work-horse children on macOS
        Apple Silicon: libdispatch (GCD) is used for sqlite3_initialize()
        via dispatch_once, and per-connection os_unfair_lock mutexes are
        also not fork-safe.  Both sqlite3.connect() and cursor.execute()
        crash with SIGSEGV inside a forked child.

        We query the booking DB here — in the parent process, which is
        completely fork-safe — and store the results in Redis so the child
        task functions can read them without touching SQLite at all.
        """
        self._pre_cache_booking_state(job)
        super().execute_job(job, queue)

    def _pre_cache_booking_state(self, job: RqJob) -> None:
        """Queries booking state and writes it to Redis before os.fork()."""
        try:
            from sqlalchemy.pool import NullPool
            from sqlmodel import create_engine

            from app.libs.queues.dtos import get_queue_context
            from app.services.booking.models import Booking
            from app.services.booking.service import (
                get_active_booking,
                get_booking,
                get_next_booking,
            )

            context = get_queue_context()
            prefix = context["queue_prefix"]
            booking_db_url = context["booking_db_url"]
            redis = self.connection

            # NullPool: no background threads; fresh connection per call —
            # safe in the parent process.
            engine = create_engine(booking_db_url, poolclass=NullPool)

            # --- next_booking ---
            try:
                next_booking = get_next_booking(engine)
                if next_booking is not None:
                    redis.set(
                        f"{prefix}:_cache:next_booking",
                        json.dumps({"start_utc": next_booking.start_utc.isoformat()}),
                        ex=300,
                    )
                else:
                    redis.delete(f"{prefix}:_cache:next_booking")
            except Exception as exc:
                logging.warning(f"[booking cache] next_booking query failed: {exc}")

            # --- active_booking ---
            try:
                active_booking = get_active_booking(engine)
                if active_booking is not None:
                    redis.set(
                        f"{prefix}:_cache:active_booking",
                        json.dumps(
                            {
                                "id": active_booking.id,
                                "end_utc": active_booking.end_utc.isoformat(),
                                "user_id": active_booking.user_id,
                                "total_duration": active_booking.total_duration,
                                "idle_timer_id": active_booking.idle_timer_id,
                            }
                        ),
                        ex=300,
                    )
                else:
                    redis.delete(f"{prefix}:_cache:active_booking")
            except Exception as exc:
                logging.warning(f"[booking cache] active_booking query failed: {exc}")

            # --- specific booking by ID (for post_booking_cleanup, reset_idleness_timer, etc.) ---
            booking_id = (job.kwargs or {}).get("booking_id")
            if booking_id:
                try:
                    booking = get_booking(engine, Booking.id == booking_id)
                    if booking is not None:
                        redis.set(
                            f"{prefix}:_cache:booking:{booking_id}",
                            json.dumps(
                                {
                                    "id": booking.id,
                                    "user_id": booking.user_id,
                                    "total_duration": booking.total_duration,
                                    "idle_timer_id": booking.idle_timer_id,
                                    "start_utc": booking.start_utc.isoformat(),
                                    "end_utc": booking.end_utc.isoformat(),
                                }
                            ),
                            ex=300,
                        )
                    else:
                        redis.delete(f"{prefix}:_cache:booking:{booking_id}")
                except Exception as exc:
                    logging.warning(
                        f"[booking cache] booking:{booking_id} query failed: {exc}"
                    )
        except Exception as exc:
            logging.warning(f"[booking cache] pre-cache failed entirely: {exc}")


class LoggingRqWorker(PreloadedRqWorker):
    """A special RQ worker that logs its messages for ease of tracking"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

        root = logging.getLogger()
        # avoid duplicate handlers if RQ restarts a child, etc.
        if not root.handlers:
            h = logging.StreamHandler(sys.stdout)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
            )
            h.setFormatter(fmt)
            root.addHandler(h)
        root.setLevel(level)
