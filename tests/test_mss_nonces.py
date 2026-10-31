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
from sqlalchemy import Engine

from services.external.mss import claim_mss_nonce, delete_expired_mss_nonces
from utils.sql_db import get_sql_engine, migrate_sql_db


def test_nonce_is_claimed_once(tmp_path):
    db_engine = _get_db_engine(tmp_path)

    claims = [claim_mss_nonce(db_engine, "device", "nonce", ttl=60) for _ in range(2)]

    assert claims == [True, False]


def test_an_expired_nonce_is_swept_and_can_be_claimed_again(tmp_path):
    db_engine = _get_db_engine(tmp_path)

    with time_machine.travel(
        datetime(2026, 1, 1, tzinfo=timezone.utc), tick=False
    ) as clock:
        claim_mss_nonce(db_engine, "device", "expired", ttl=60)
        claim_mss_nonce(db_engine, "device", "current", ttl=120)
        clock.shift(61)
        delete_expired_mss_nonces(db_engine)
        claims = [
            claim_mss_nonce(db_engine, "device", nonce, ttl=60)
            for nonce in ("expired", "current")
        ]

    assert claims == [True, False]


def test_nonce_of_another_device_is_its_own(tmp_path):
    db_engine = _get_db_engine(tmp_path)

    claims = [
        claim_mss_nonce(db_engine, device_name, "nonce", ttl=60)
        for device_name in ("device-a", "device-b")
    ]

    assert claims == [True, True]


def _get_db_engine(tmp_path) -> Engine:
    url = f"sqlite:///{tmp_path}/bcc.db"
    migrate_sql_db(url)
    return get_sql_engine(url)
