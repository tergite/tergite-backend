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
"""Utilities for the bookings the tests make through the API"""
from datetime import datetime, timedelta, timezone
from typing import (
    Optional,
    Tuple,
    TypedDict,
)

from fastapi.testclient import TestClient
from httpx import Response

from tests.utils.api import (
    create_mss_headers,
)
from tests.utils.params import BasicBookingInfo


def cancel_booking(
    client: "TestClient", user_id: str, booking_id: str, is_admin: Optional[bool] = None
) -> "Response":
    """Cancels the booking on the API

    Args:
        client: the fastapi TestClient for running tests
        user_id: the user ID for authentication
        booking_id: the unique identifier of the booking
        is_admin: whether to visit the cancellation endpoint as an MSS admin or not

    Returns:
        the httpx.Response from the cancellation endpoint
    """
    headers = create_mss_headers(user_id, is_admin=is_admin)
    return client.post(f"/bookings/{booking_id}/cancel", headers=headers)


def view_booking_list(
    client: "TestClient",
    requester_id: str,
    skip: int = 0,
    limit: Optional[int] = None,
    sort: Tuple[str, ...] = (),
    is_admin: Optional[bool] = None,
    **filters,
) -> "Response":
    """Views the paginated list of bookings

    Args:
        client: the test client in which the tests run
        requester_id: the user ID for authentication
        skip: the number of records to skip
        limit: the maximum number of records to return
        sort: the columns to sort by, prepending "-" to mean descending order
        is_admin: whether to access the bookings endpoint as admin
        filters: additional filters to apply to the bookings

    Returns:
        the httpx.Response for the request
    """
    headers = create_mss_headers(requester_id, is_admin=is_admin)
    params = {"skip": skip, "sort": sort}
    if isinstance(limit, int):
        params["limit"] = limit
    params.update(filters)

    return client.get("/bookings", headers=headers, params=params)


def to_booking_payload(booking_info: "BasicBookingInfo") -> "BookingPayload":
    """Converts the booking info to payload for creation of a new booking

    Args:
        booking_info: the basic booking info containing starts_in, duration

    Returns:
        the payload for creating a booking
    """
    starts_in = timedelta(seconds=booking_info["starts_in"])
    duration = timedelta(seconds=booking_info["duration"])

    current_timestamp = datetime.now(timezone.utc)
    start_utc = current_timestamp + starts_in
    end_utc = start_utc + duration
    return {
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
    }


def post_booking(
    client: "TestClient", user_id: str, start_utc: datetime, end_utc: datetime
) -> "Response":
    """Books the slot from start_utc to end_utc for the user"""
    return client.post(
        "/bookings",
        headers=create_mss_headers(user_id),
        json={"start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat()},
    )


def create_booking(
    client: "TestClient", user_id: str, booking: "BasicBookingInfo"
) -> Tuple["BasicBookingInfo", "Response"]:
    """Creates the booking and returns the actual booking details

    It assumes the user is a valid user in MSS

    Actual booking details include actual duration and actual delay

    Args:
        client: the fastapi TestClient used for testing
        user_id: the id of the user
        booking: the basic booking info

    Returns:
        the tuple of the actual basic info of the created booking and the httpx.Response
    """
    headers = create_mss_headers(user_id)
    current_timestamp = datetime.now(timezone.utc)
    data = to_booking_payload(booking)
    response = client.post("/bookings", headers=headers, json=data)
    json_response = response.json()

    start_utc = json_response["start_utc"]
    start_utc_datetime = datetime.fromisoformat(start_utc)
    end_utc = json_response["end_utc"]
    end_utc_datetime = datetime.fromisoformat(end_utc)

    actual_duration = (end_utc_datetime - start_utc_datetime).total_seconds()
    actual_delay = (start_utc_datetime - current_timestamp).total_seconds()
    # round off delay just to shave off the milliseconds lost during execution
    actual_delay = int(actual_delay)

    actual_basic_info: BasicBookingInfo = {
        "duration": actual_duration,
        "starts_in": actual_delay,
    }
    return actual_basic_info, response


class BookingPayload(TypedDict):
    """The payload for creation of a booking"""

    start_utc: str
    end_utc: str
