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
    """Claims the nonce MSS signed a request with, so that the request cannot be replayed

    A nonce is kept for ttl seconds, past which the timestamp MSS signed with it is refused
    as too old anyway; it stays claimed until delete_expired_mss_nonces sweeps it.

    Args:
        db_engine: the SQL database the nonces are kept in
        device_name: the name of the device the request was sent to
        nonce: the request's `x-mss-request-id`
        ttl: the seconds to keep the nonce for, the device's `mss_nonce_ttl`

    Returns:
        whether the nonce was new; False if another request claimed it first
    """
    with Session(db_engine) as session:
        session.add(
            MssNonceRow(
                device_name=device_name,
                nonce=nonce,
                expires_at=get_utc_now() + timedelta(seconds=ttl),
            )
        )
        try:
            session.commit()
        except IntegrityError:
            return False
    return True


def delete_expired_mss_nonces(db_engine: Engine) -> None:
    """Deletes the nonces whose ttl has passed, which no request could replay anyway

    Args:
        db_engine: the SQL database the nonces are kept in
    """
    with Session(db_engine) as session:
        session.exec(
            delete(MssNonceRow).where(col(MssNonceRow.expires_at) < get_utc_now())
        )
        session.commit()
