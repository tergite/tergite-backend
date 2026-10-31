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
"""The events MSS could not be sent, kept in the SQL database in order until it can be"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import Engine
from sqlmodel import Field, Session, SQLModel, col, delete, select

from utils.datetime import get_utc_now, to_utc
from utils.exc import MssRejectedError, MssTimeoutError
from utils.sql_db import open_immediate_session

from .dtos import DeviceEvent
from .service import MssClient


class MssEventRow(SQLModel, table=True):
    """An event kept for MSS, until MSS has answered it"""

    __tablename__ = "mss_event"

    # the order the events were kept in, which they are sent in
    sequence: Optional[int] = Field(default=None, primary_key=True)
    id: str = Field(unique=True)
    device_name: str = Field(index=True)
    name: str
    data: str
    # when a process began sending it, so that no other sends it meanwhile
    claimed_at: Optional[datetime] = None


def send_mss_event(
    db_engine: Engine, mss_client: MssClient, event: DeviceEvent
) -> None:
    """Sends the event to MSS, or keeps it for send_kept_mss_events: when MSS cannot be
    reached, or other events of the device wait to be sent before it. MSS's refusal is
    logged, as sending it again would not change MSS's answer."""
    if _is_any_event_kept(db_engine, mss_client.device):
        _keep_event(db_engine, mss_client.device, event)
        return
    try:
        mss_client.send_event(event)
    except MssTimeoutError as exp:
        logging.warning(f"kept {event.name.value} event {event.id} for MSS: {exp}")
        _keep_event(db_engine, mss_client.device, event)
    except MssRejectedError as exp:
        logging.error(f"MSS refused {event.name.value} event {event.id}: {exp}")


def send_kept_mss_events(
    db_engine: Engine, mss_client: MssClient, max_reconnections: Optional[int] = None
) -> bool:
    """Sends MSS the device's kept events, oldest first, until it cannot be reached, unless
    another process is sending them; MSS's refusals are logged and dropped

    Args:
        db_engine: the SQL database the events are kept in
        mss_client: the client to the device's MSS
        max_reconnections: the most times to reconnect per event; default: the client's

    Returns:
        whether MSS could be reached
    """
    # past it, a sender is taken to have died, so its claim lapses
    claim_ttl = timedelta(seconds=2 * mss_client.response_timeout)
    while True:
        row = _claim_oldest_event(db_engine, mss_client.device, claim_ttl=claim_ttl)
        if row is None:
            return True
        try:
            mss_client.send_event_json(
                row.id, row.data, max_reconnections=max_reconnections
            )
        except MssTimeoutError as exp:
            logging.warning(
                f"MSS cannot take the kept {row.name} event {row.id}: {exp}"
            )
            _clear_claim(db_engine, row.sequence)
            return False
        except MssRejectedError as exp:
            logging.error(f"MSS refused the kept {row.name} event {row.id}: {exp}")
        _delete_event(db_engine, row.sequence)


def _is_any_event_kept(db_engine: Engine, device_name: str) -> bool:
    with Session(db_engine) as session:
        sequence = session.exec(
            select(MssEventRow.sequence).where(
                col(MssEventRow.device_name) == device_name
            )
        ).first()
    return sequence is not None


def _keep_event(db_engine: Engine, device_name: str, event: DeviceEvent) -> None:
    with Session(db_engine) as session:
        session.add(
            MssEventRow(
                id=event.id,
                device_name=device_name,
                name=event.name.value,
                data=event.model_dump_json(),
            )
        )
        session.commit()


def _claim_oldest_event(
    db_engine: Engine, device_name: str, claim_ttl: timedelta
) -> Optional[MssEventRow]:
    """Claims the device's oldest kept event, unless another process's claim on it holds,
    so that the events reach MSS one at a time, in order"""
    now = get_utc_now()
    with open_immediate_session(db_engine) as session:
        row = session.exec(
            select(MssEventRow)
            .where(col(MssEventRow.device_name) == device_name)
            .order_by(col(MssEventRow.sequence))
        ).first()
        if row is None:
            return None
        if row.claimed_at is not None and to_utc(row.claimed_at) > now - claim_ttl:
            return None
        row.claimed_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _clear_claim(db_engine: Engine, sequence: Optional[int]) -> None:
    with Session(db_engine) as session:
        row = session.get(MssEventRow, sequence)
        if row is not None:
            row.claimed_at = None
            session.add(row)
            session.commit()


def _delete_event(db_engine: Engine, sequence: Optional[int]) -> None:
    with Session(db_engine) as session:
        session.exec(delete(MssEventRow).where(col(MssEventRow.sequence) == sequence))
        session.commit()
