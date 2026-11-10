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
"""Tests for the SQL engines the devices' stores run on"""

import threading

from tests.utils.env import TEST_BOOKING_DB_URL
from utils.sql_db import get_sql_engine, open_immediate_session


def test_a_sqlite_file_is_written_ahead_and_synced_at_checkpoints(tmp_path):
    """So that a commit, as of each request's nonce, does not wait on the disk"""
    engine = get_sql_engine(f"sqlite:///{tmp_path}/bcc.db")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()

    # 1 is NORMAL
    assert (journal_mode, synchronous) == ("wal", 1)


def test_an_immediate_session_waits_for_another_to_end():
    """So that what it reads cannot change before it writes, on each database the
    tests run on"""
    db_engine = get_sql_engine(TEST_BOOKING_DB_URL)
    is_second_open = threading.Event()

    def open_second_session() -> None:
        with open_immediate_session(db_engine):
            is_second_open.set()

    second_session = threading.Thread(target=open_second_session)
    with open_immediate_session(db_engine):
        second_session.start()
        is_second_open_meanwhile = is_second_open.wait(timeout=1)
    second_session.join()

    assert not is_second_open_meanwhile
    assert is_second_open.is_set()
