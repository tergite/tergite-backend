# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Routes for the bookings of the device"""
from datetime import datetime
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Engine

from services import booking, scheduler
from services.booking import Booking, BookingsConfig, NewBookingInfo
from utils.messages import GeneralMessage
from utils.sql_db import convert_http_sort_to_db_sort

from ..dependencies import (
    DeviceResourcesDep,
    check_device_is_online,
    get_booking_db,
    get_unverified_mss_is_admin,
    get_verified_mss_user_id,
)
from ..dtos import PaginatedListResponse

router = APIRouter()


@router.post("/bookings", dependencies=[Depends(check_device_is_online)])
async def create_booking(
    device: DeviceResourcesDep,
    booking_info: NewBookingInfo,
    user_id: str = Depends(get_verified_mss_user_id),
) -> Booking:
    """Creates a booking for the user of the given token

    Args:
        device: what the device runs on
        user_id: the MSS user_id as sent by MSS
        booking_info: the information about the new booking

    Returns:
        the newly created booking
    """
    booking.get_or_create_user(device.bookings_db, user_id)

    return scheduler.submit_booking(device, user_id=user_id, booking_info=booking_info)


@router.post(
    "/bookings/{booking_id}/cancel",
    dependencies=[Depends(check_device_is_online)],
)
async def cancel_booking(
    device: DeviceResourcesDep,
    booking_id: str,
    user_id: str = Depends(get_verified_mss_user_id),
    is_mss_admin: bool = Depends(get_unverified_mss_is_admin),
) -> GeneralMessage:
    """Cancels a booking of given id for the user of the given token

    Args:
        device: what the device runs on
        booking_id: the unique identifier of the booking to cancel
        user_id: the MSS user_id as sent by MSS
        is_mss_admin: whether the user is an admin in MSS or not

    Returns:
        the general message object with the status
    """
    scheduler.cancel_booking(
        device, user_id=user_id, booking_id=booking_id, is_mss_admin=is_mss_admin
    )
    return {"status": "success", "detail": f"Booking of id {booking_id} cancelled"}


@router.get("/bookings", dependencies=[Depends(get_verified_mss_user_id)])
async def view_bookings(
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
    sort: Tuple[str, ...] = Query(default=()),
    min_start_utc: Optional[datetime] = Query(default=None),
    max_start_utc: Optional[datetime] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    db_engine: Engine = Depends(get_booking_db),
) -> PaginatedListResponse[Booking]:
    """Views all available bookings

    Args:
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.
        sort: fields to sort by; prepending "-" returns the records in descending order
        db_engine: the SQL database engine to query
        user_id: the unique identifier of the owner of the given bookings
        min_start_utc: the minimum start time in UTC
        max_start_utc: the maximum start time in UTC

    Returns:
        the paginated list of the available bookings
    """
    filters = []
    if min_start_utc is not None:
        filters.append(Booking.start_utc >= min_start_utc)
    if max_start_utc is not None:
        filters.append(Booking.start_utc <= max_start_utc)
    if user_id is not None:
        filters.append(Booking.user_id == user_id)

    sort = convert_http_sort_to_db_sort(Booking, http_sort=sort)
    bookings = booking.get_many_bookings(
        db_engine, *filters, skip=skip, limit=limit, sort=sort
    )
    return PaginatedListResponse(skip=skip, limit=limit, data=bookings)


@router.get("/bookings/config", dependencies=[Depends(get_verified_mss_user_id)])
async def view_bookings_config(device: DeviceResourcesDep) -> BookingsConfig:
    """Views the configurations for the bookings service

    Args:
        device: what the device runs on

    Returns:
        the instance of the bookings configurations
    """
    return BookingsConfig.from_settings(device.settings)
