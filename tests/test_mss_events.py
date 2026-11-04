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
"""Tests for the events kept for MSS until it can be reached"""

from datetime import datetime, timedelta, timezone

import pytest
import time_machine
from sqlmodel import Session, select

from services.external.mss import send_kept_mss_events, send_mss_event
from services.external.mss.events import MssEventRow
from tests.utils.mss import get_mss_client, get_switched_on_event
from utils.sql_db import get_sql_engine, migrate_sql_db


@pytest.fixture(autouse=True)
def _skip_reconnection_delays(mocker):
    mocker.patch("time.sleep")


@pytest.fixture
def db_engine(tmp_path):
    url = f"sqlite:///{tmp_path}/bcc.db"
    migrate_sql_db(url)
    return get_sql_engine(url)


def test_event_mss_cannot_take_is_kept_until_it_can(db_engine, mock_mss):
    client = get_mss_client()
    event = get_switched_on_event()

    mock_mss.is_unreachable = True
    send_mss_event(db_engine, client, event)
    is_reached_while_down = send_kept_mss_events(db_engine, client)
    mock_mss.is_unreachable = False
    is_reached = send_kept_mss_events(db_engine, client)

    assert (is_reached_while_down, is_reached) == (False, True)
    assert [sent["id"] for sent in mock_mss.sent_events] == [event.id]
    assert _get_kept_event_ids(db_engine) == []


def test_event_waits_behind_the_kept_ones(db_engine, mock_mss):
    client = get_mss_client()
    events = [get_switched_on_event() for _ in range(3)]

    mock_mss.is_unreachable = True
    send_mss_event(db_engine, client, events[0])
    mock_mss.is_unreachable = False
    send_mss_event(db_engine, client, events[1])
    send_mss_event(db_engine, client, events[2])

    assert mock_mss.sent_events == []
    send_kept_mss_events(db_engine, client)
    assert [sent["id"] for sent in mock_mss.sent_events] == [
        event.id for event in events
    ]


def test_event_mss_refuses_is_dropped(db_engine, mock_mss):
    client = get_mss_client()
    mock_mss.rejected_event_names.add("switched_on")

    send_mss_event(db_engine, client, get_switched_on_event())

    assert _get_kept_event_ids(db_engine) == []


def test_kept_event_another_sender_claimed_is_left_to_it_until_its_claim_lapses(
    db_engine, mock_mss
):
    client = get_mss_client()
    event = get_switched_on_event()
    mock_mss.is_unreachable = True
    send_mss_event(db_engine, client, event)
    mock_mss.is_unreachable = False
    with Session(db_engine) as session:
        row = session.exec(select(MssEventRow)).one()
        row.claimed_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()

    send_kept_mss_events(db_engine, client)
    sent_while_claimed = list(mock_mss.sent_events)
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(minutes=1)):
        send_kept_mss_events(db_engine, client)

    assert sent_while_claimed == []
    assert [sent["id"] for sent in mock_mss.sent_events] == [event.id]


def _get_kept_event_ids(db_engine) -> list[str]:
    with Session(db_engine) as session:
        return list(session.exec(select(MssEventRow.id)).all())
