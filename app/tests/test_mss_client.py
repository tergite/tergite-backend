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
"""Tests for the client that sends the device's events to MSS"""
from pathlib import Path

import pytest

from app.services.external.mss.auth import get_private_key
from app.services.external.mss.dtos import (
    DeviceEvent,
    DeviceEventName,
    DeviceSwitchData,
)
from app.services.external.mss.service import MssClient
from app.tests.utils.env import TEST_MSS_PRIVATE_KEY_PATH, TEST_PRIVATE_KEY_FILE
from app.utils.datetime import get_utc_now
from app.utils.exc import MssRejectedError, MssTimeoutError


@pytest.fixture(autouse=True)
def _skip_reconnection_delays(mocker):
    mocker.patch("time.sleep")


def test_event_is_sent_and_answered(mock_mss):
    event = _get_switched_on_event()

    response = _get_mss_client().send_event(event)

    assert response["status"] == "success"
    assert [sent["id"] for sent in mock_mss.sent_events] == [event.id]


def test_event_mss_rejects_raises(mock_mss):
    mock_mss.rejected_event_names.add("switched_on")

    with pytest.raises(MssRejectedError, match="rejected"):
        _get_mss_client().send_event(_get_switched_on_event())


def test_event_mss_does_not_answer_times_out(mock_mss):
    mock_mss.is_silent = True

    with pytest.raises(MssTimeoutError):
        _get_mss_client().send_event(_get_switched_on_event())


def test_connection_dropped_while_waiting_is_reopened_but_loses_the_reply(mock_mss):
    mock_mss.should_drop_next_recv = True

    with pytest.raises(MssTimeoutError):
        _get_mss_client().send_event(_get_switched_on_event())

    assert len(mock_mss.connections) == 2


def test_connection_signed_with_another_key_is_refused(mock_mss):
    client = _get_mss_client(private_key_file=TEST_MSS_PRIVATE_KEY_PATH)

    with pytest.raises(MssTimeoutError, match="maximum connection attempts"):
        client.send_event(_get_switched_on_event())

    assert mock_mss.connections == []


def _get_mss_client(private_key_file: Path = TEST_PRIVATE_KEY_FILE) -> MssClient:
    return MssClient(
        uri="ws://mss.test/devices/ws/loke",
        device="loke",
        private_key=get_private_key(private_key_file, password=None),
        response_timeout=1,
        max_reconnections=2,
    )


def _get_switched_on_event() -> DeviceEvent:
    return DeviceEvent(
        name=DeviceEventName.SWITCHED_ON, data=DeviceSwitchData(date=get_utc_now())
    )
