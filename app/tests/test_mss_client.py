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
"""Tests for the client that sends the device's events to MSS

The app's modules are imported in each test, not here: test_settings re-imports app, and
mock_mss patches the module as it is then.
"""
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from app.tests.utils.env import TEST_MSS_PRIVATE_KEY_PATH, TEST_PRIVATE_KEY_FILE

if TYPE_CHECKING:
    from app.services.external.mss.dtos import DeviceEvent
    from app.services.external.mss.service import MssClient


@pytest.fixture(autouse=True)
def _skip_reconnection_delays(mocker):
    mocker.patch("time.sleep")


@pytest.fixture
def exc() -> ModuleType:
    from app.utils import exc

    return exc


def test_event_is_sent_and_answered(mock_mss):
    event = _get_switched_on_event()

    response = _get_mss_client().send_event(event)

    assert response["status"] == "success"
    assert [sent["id"] for sent in mock_mss.sent_events] == [event.id]


def test_event_mss_rejects_raises(mock_mss, exc):
    mock_mss.rejected_event_names.add("switched_on")

    with pytest.raises(exc.MssRejectedError, match="rejected"):
        _get_mss_client().send_event(_get_switched_on_event())


def test_event_mss_does_not_answer_times_out(mock_mss, exc):
    mock_mss.is_silent = True

    with pytest.raises(exc.MssTimeoutError):
        _get_mss_client().send_event(_get_switched_on_event())


def test_connection_dropped_while_waiting_is_reopened_but_loses_the_reply(
    mock_mss, exc
):
    mock_mss.should_drop_next_recv = True

    with pytest.raises(exc.MssTimeoutError):
        _get_mss_client().send_event(_get_switched_on_event())

    assert len(mock_mss.connections) == 2


def test_connection_signed_with_another_key_is_refused(mock_mss, exc):
    client = _get_mss_client(private_key_file=TEST_MSS_PRIVATE_KEY_PATH)

    with pytest.raises(exc.MssTimeoutError, match="maximum connection attempts"):
        client.send_event(_get_switched_on_event())

    assert mock_mss.connections == []


def _get_mss_client(private_key_file: Path = TEST_PRIVATE_KEY_FILE) -> "MssClient":
    from app.services.external.mss.service import MssClient

    return MssClient(
        uri="ws://mss.test/devices/ws/loke",
        device="loke",
        private_key_file=private_key_file,
        response_timeout=1,
        max_reconnections=2,
    )


def _get_switched_on_event() -> "DeviceEvent":
    from app.services.external.mss.dtos import (
        DeviceEvent,
        DeviceEventName,
        DeviceSwitchData,
    )
    from app.utils.datetime import get_utc_now

    return DeviceEvent(
        name=DeviceEventName.SWITCHED_ON, data=DeviceSwitchData(date=get_utc_now())
    )
