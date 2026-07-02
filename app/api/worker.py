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
import logging
import os
import sys

from rq import Worker


class PreloadedRqWorker(Worker):
    """RQ Worker that has important libraries, connections etc. preloaded."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # initialize connections and other globals,
        # so that they are available to forked child processes
        from app.libs.queues.dtos import get_queue_context
        from app.services.booking.store import get_bookings_sql_engine
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
        _db_engine = get_bookings_sql_engine()


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
