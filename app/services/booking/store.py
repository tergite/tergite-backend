# This code is part of Tergite
#
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
"""Module the store"""
import os

from sqlalchemy import Engine

import settings

from ...utils.sql_db import get_sql_engine
from .models import Booking, User


def init_booking_db(url: str = settings.BOOKING_DB_URL) -> Engine:
    """Gets (or creates) the SQLAlchemy engine for the bookings service.

    The result is cached so that forked child processes inherit the already-open
    engine and don't need to call sqlite3.connect() themselves — which would
    crash on macOS after fork() in a multithreaded process.

    Args:
        url: the database URL for the database
    """
    return get_sql_engine(url=f"{url}", models=[Booking, User])
