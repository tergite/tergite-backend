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

from utils.sql_db import get_sql_engine


def test_a_sqlite_file_is_written_ahead_and_synced_at_checkpoints(tmp_path):
    """So that a commit, as of each request's nonce, does not wait on the disk"""
    engine = get_sql_engine(f"sqlite:///{tmp_path}/bcc.db")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()

    # 1 is NORMAL
    assert (journal_mode, synchronous) == ("wal", 1)
