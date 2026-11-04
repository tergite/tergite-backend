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
"""Utilities for reading the recalibrations the dispatcher keeps"""

import os
from typing import List

from sqlalchemy import create_engine
from sqlmodel import Session, col, select

from services.scheduler.store import RecalibrationRow
from tests.utils.env import TEST_BOOKING_DB_URL
from utils.datetime import get_utc_now


def get_recalibrations() -> List[RecalibrationRow]:
    """Gets the test device's recalibrations, earliest due first"""
    with Session(create_engine(TEST_BOOKING_DB_URL)) as session:
        return list(
            session.exec(
                select(RecalibrationRow)
                .where(
                    col(RecalibrationRow.device_name)
                    == os.environ["BCC_DEFAULT_PREFIX"]
                )
                .order_by(col(RecalibrationRow.due_at))
            )
        )


def start_due_recalibration_without_running_it(device_name: str) -> None:
    """Leaves the device's due recalibration started, as a worker that died running it does"""
    with Session(create_engine(TEST_BOOKING_DB_URL)) as session:
        row = session.exec(
            select(RecalibrationRow)
            .where(
                col(RecalibrationRow.device_name) == device_name,
                col(RecalibrationRow.status) == "scheduled",
            )
            .order_by(col(RecalibrationRow.due_at))
        ).first()
        row.status, row.started_at = "started", get_utc_now()
        session.add(row)
        session.commit()
