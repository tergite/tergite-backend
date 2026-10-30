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
"""Tests for the claims on MSS's nonces"""
from datetime import datetime, timezone

import time_machine

from services.external.mss import claim_mss_nonce
from utils.sql_db import get_sql_engine, migrate_sql_db


def test_nonce_is_claimed_once_until_it_expires(tmp_path):
    url = f"sqlite:///{tmp_path}/bcc.db"
    migrate_sql_db(url)
    db_engine = get_sql_engine(url)

    with time_machine.travel(
        datetime(2026, 1, 1, tzinfo=timezone.utc), tick=False
    ) as clock:
        claims = [
            claim_mss_nonce(db_engine, "device", "nonce", ttl=60) for _ in range(2)
        ]
        clock.shift(61)
        claims.append(claim_mss_nonce(db_engine, "device", "nonce", ttl=60))

    assert claims == [True, False, True]


def test_nonce_of_another_device_is_its_own(tmp_path):
    url = f"sqlite:///{tmp_path}/bcc.db"
    migrate_sql_db(url)
    db_engine = get_sql_engine(url)

    claims = [
        claim_mss_nonce(db_engine, device_name, "nonce", ttl=60)
        for device_name in ("device-a", "device-b")
    ]

    assert claims == [True, True]
