# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2023
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Test utilities for datetime"""
from datetime import datetime, timezone
from typing import (
    Any,
    Generator,
    Literal,
)

import pytest
import time_machine

from app.tests.utils.executors import durations

_TimeSpec = Literal[
    "auto", "hours", "minutes", "seconds", "milliseconds", "microseconds"
]


def get_timestamp_str(timestamp: datetime, timespec: _TimeSpec = "milliseconds") -> str:
    """Converts a timestamp to a string

    Args:
        timestamp: the datetime value
        timespec: the number of additional terms of the time to include. Valid options are 'auto', 'hours',
            'minutes', 'seconds', 'milliseconds' and 'microseconds'

    Returns:
        the timestamp as a string
    """
    return timestamp.isoformat("T", timespec=timespec).replace("+00:00", "Z")


def get_current_timestamp_str() -> str:
    """Gets the current timestamp as a Zulu ISO format string

    Returns:
        the timestamp as a Zulu ISO format string
    """
    timestamp = datetime.now(timezone.utc)
    return get_timestamp_str(timestamp)


@pytest.fixture
def fake_clock(mocker) -> Generator[time_machine.Traveller, Any, None]:
    """A clock that jumps ahead instead of waiting: the test jobs shift it by
    their duration, and wait_on_worker moves it on to the next change in the bookings"""
    with time_machine.travel(datetime.now(timezone.utc), tick=True) as clock:
        mocker.patch.object(durations, "wait", side_effect=clock.shift)
        yield clock
