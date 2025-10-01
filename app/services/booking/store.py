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

from sqlalchemy import Engine

from ...utils.sql_db import get_sql_engine
from .models import Booking, User


def get_bookings_sql_engine(url: str) -> Engine:
    """Gets the SQLAlchemy engine for the bookings service

    Args:
        url: the database URL for the database
    """
    return get_sql_engine(url=url, models=[Booking, User])
