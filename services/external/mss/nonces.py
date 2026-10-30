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
"""The nonces of MSS's signed requests, in the SQL database, so that none is replayed"""
from datetime import datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, col, delete

from utils.datetime import get_utc_now


class MssNonceRow(SQLModel, table=True):
    """A nonce a device has seen, until it expires"""

    __tablename__ = "mss_nonce"

    device_name: str = Field(primary_key=True)
    nonce: str = Field(primary_key=True)
    expires_at: datetime = Field(index=True)


def claim_mss_nonce(
    db_engine: Engine, device_name: str, nonce: str, ttl: float
) -> bool:
    """Claims the nonce for the device, for the ttl in seconds, deleting the expired ones

    Returns:
        whether the nonce was new, as no other request has claimed it
    """
    now = get_utc_now()
    with Session(db_engine) as session:
        session.exec(delete(MssNonceRow).where(col(MssNonceRow.expires_at) < now))
        session.add(
            MssNonceRow(
                device_name=device_name,
                nonce=nonce,
                expires_at=now + timedelta(seconds=ttl),
            )
        )
        try:
            session.commit()
        except IntegrityError:
            return False
    return True
