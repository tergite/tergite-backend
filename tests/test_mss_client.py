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

import itertools
from types import SimpleNamespace

import pytest

from tests.utils.env import TEST_MSS_PRIVATE_KEY_PATH
from tests.utils.mss import get_mss_client, get_switched_on_event
from utils.exc import MssRejectedError, MssTimeoutError


@pytest.fixture(autouse=True)
def _skip_reconnection_delays(mocker):
    mocker.patch("time.sleep")


def test_event_is_sent_and_answered(mock_mss):
    event = get_switched_on_event()

    response = get_mss_client().send_event(event)

    assert response["status"] == "success"
    assert [sent["id"] for sent in mock_mss.sent_events] == [event.id]


def test_event_mss_rejects_raises(mock_mss):
    mock_mss.rejected_event_names.add("switched_on")

    with pytest.raises(MssRejectedError, match="rejected"):
        get_mss_client().send_event(get_switched_on_event())


def test_event_mss_does_not_answer_times_out(mock_mss):
    mock_mss.is_silent = True

    with pytest.raises(MssTimeoutError):
        get_mss_client().send_event(get_switched_on_event())


def test_event_whose_connection_dropped_before_its_reply_is_resent(mock_mss):
    """The reply on the dropped connection is lost with it, so the event is sent again"""
    mock_mss.should_drop_next_recv = True
    event = get_switched_on_event()

    response = get_mss_client().send_event(event)

    assert response["status"] == "success"
    assert [
        [sent["id"] for sent in connection.sent_events]
        for connection in mock_mss.connections
    ] == [[event.id], [event.id]]


def test_connection_signed_with_another_key_is_refused(mock_mss):
    client = get_mss_client(private_key_file=TEST_MSS_PRIVATE_KEY_PATH)

    with pytest.raises(MssTimeoutError, match="maximum connection attempts"):
        client.send_event(get_switched_on_event())

    assert mock_mss.connections == []


def test_reply_is_awaited_for_what_is_left_of_the_timeout(mocker):
    """Each message that is not the reply leaves less of the timeout to wait in"""
    # a clock that moves on 0.1 s at each reading
    mocker.patch(
        "services.external.mss.service.time.monotonic",
        side_effect=itertools.count(step=0.1),
    )
    event = get_switched_on_event()
    timeouts = []
    messages = iter(
        [
            b'{"id": "other"}',
            b"not json",
            f'{{"id": "{event.id}", "status": "success"}}',
        ]
    )

    def recv(decode: bool, timeout: float) -> bytes:
        timeouts.append(timeout)
        return next(messages)

    client = get_mss_client()
    client._client = SimpleNamespace(send=lambda *_, **__: None, recv=recv)

    response = client.send_event(event)

    assert response["status"] == "success"
    assert timeouts[0] < 1
    assert all(earlier > later for earlier, later in itertools.pairwise(timeouts))
