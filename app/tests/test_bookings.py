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
"""Tests for the bookings service"""
import threading
import time
from datetime import timedelta

from sqlmodel import Session, select

from app.services.booking import service as booking_service
from app.services.booking.models import Booking, NewBookingInfo, NewUserInfo, User
from app.utils.datetime import get_utc_now
from app.utils.exc import ConflictError
from app.utils.sql_db import get_sql_engine


def test_concurrent_overlapping_bookings_book_the_slot_once(tmp_path, mocker):
    db_engine = get_sql_engine(f"sqlite:///{tmp_path}/bookings.db", [Booking, User])
    start_utc = get_utc_now() + timedelta(minutes=5)
    booking_info = NewBookingInfo(
        start_utc=start_utc, end_utc=start_utc + timedelta(minutes=10)
    )
    check_overlap = booking_service._validate_no_overlap

    def check_overlap_slowly(*args, **kwargs):
        check_overlap(*args, **kwargs)
        # the other request checks while this one has yet to insert
        time.sleep(0.5)

    mocker.patch.object(
        booking_service, "_validate_no_overlap", side_effect=check_overlap_slowly
    )
    errors = []

    def book(user_id: str):
        try:
            booking_service.create_booking(db_engine, user_id, data=booking_info)
        except ConflictError as exp:
            errors.append(exp)

    threads = [threading.Thread(target=book, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(booking_service.get_many_bookings(db_engine)) == 1
    assert len(errors) == 1


def test_user_from_mss_keeps_no_password(tmp_path):
    db_engine = get_sql_engine(f"sqlite:///{tmp_path}/bookings.db", [Booking, User])
    data = NewUserInfo.model_validate(
        {"id": "a", "name": "A", "email": "a@example.com", "password": "secret"}
    )

    booking_service.create_user(db_engine, data=data)

    with Session(db_engine) as session:
        assert session.exec(select(User.password)).one() == ""
