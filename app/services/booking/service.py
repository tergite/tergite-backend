# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Module containing the service methods"""
import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional

import jwt
from jwt import InvalidTokenError
from sqlalchemy import Engine
from sqlalchemy.sql._typing import (
    _ColumnExpressionArgument,
    _ColumnExpressionOrStrLabelArgument,
)
from sqlmodel import Session, delete, select

import settings

from ...utils.datetime import get_day_range, get_utc_now
from ...utils.exc import (
    ConflictError,
    ItemNotFoundError,
    MaxBookingsError,
    NotAuthenticatedError,
    UnauthorizedError,
)
from ...utils.strings import get_random_name, uuid_str
from .models import (
    Booking,
    NewBookingInfo,
    NewUserInfo,
    User,
    UserProfile,
)
from .utils import hash_password

_Filter = _ColumnExpressionArgument[bool] | bool


def create_booking(db_engine: Engine, user_id: str, data: NewBookingInfo) -> Booking:
    """Creates a new booking for the given user and returns it

    Args:
        db_engine: the SQL engine to save new job in
        user_id: the id of the User who is making the booking
        data: the booking info

    Returns:
        the created booking

    Raises:
        ConflictError: booking conflicts with another booking at {start_utc} to {end_utc}
        MaxBookingsError: booking conflicts with another booking at {start_utc} to {end_utc}
    """
    with Session(db_engine) as session:
        _validate_no_overlap(session, data)
        _validate_max_slots(session, user_id=user_id, booking_info=data)

        booking = Booking(user_id=user_id, **data.model_dump())
        session.add(booking)
        session.commit()
        session.refresh(booking)
        # model validate to ensure timezone info is reloaded
        booking = Booking.model_validate(booking)
        session.expunge_all()
        return booking


def delete_bookings(db_engine: Engine, *filters: _Filter):
    """Deletes the bookings that match the given filters

    Args:
        db_engine: the SQL engine where the booking is saved
        filters: the filters that the bookings should match
    """
    with Session(db_engine) as session:
        statement = delete(Booking).where(*filters)
        session.exec(statement)
        session.commit()


def get_booking(db_engine: Engine, *filters: _Filter) -> Optional[Booking]:
    """Retrieves the first booking that matches the given filters

    Args:
        db_engine: the SQL engine to query from
        filters: the filters that the bookings should match

    Returns:
        the booking or None if it does not exist
    """
    with Session(db_engine) as session:
        statement = select(Booking).where(*filters)
        results = session.exec(statement)
        booking = results.first()
        if booking is None:
            return None

        # model validate to ensure timezone info is reloaded
        booking = Booking.model_validate(booking)
        session.expunge_all()
        return booking


def get_many_bookings(
    db_engine: Engine,
    *filters: _Filter,
    skip: int = 0,
    limit: int | None = None,
    sort: tuple[_ColumnExpressionOrStrLabelArgument[Any]] = (),
) -> List[Booking]:
    """Retrieves the bookings that match the given filters

    Args:
        db_engine: the SQL engine to query from
        filters: the filters that the bookings should match
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.
        sort: fields to sort by; default = (,)

    Returns:
        the list of bookings that match the given filters
    """
    with Session(db_engine) as session:
        statement = (
            select(Booking).where(*filters).limit(limit).offset(skip).order_by(*sort)
        )
        results = session.exec(statement)
        records = list(results.all())
        # model validate to ensure timezone info is reloaded
        records = [Booking.model_validate(v) for v in records]
        session.expunge_all()
        return records


def get_active_booking(db_engine: Engine) -> Optional[Booking]:
    """Gets the booking which is currently active

    Args:
        db_engine: the SQL engine to query from

    Returns:
        the booking or None if no active booking
    """
    with Session(db_engine) as session:
        current_timestamp = get_utc_now()
        statement = select(Booking).where(
            Booking.start_utc <= current_timestamp, Booking.end_utc >= current_timestamp
        )
        results = session.exec(statement)
        booking = results.first()
        if booking is None:
            return None

        # model validate to ensure timezone info is reloaded
        booking = Booking.model_validate(booking)
        session.expunge_all()
        return booking


def get_next_booking(db_engine: Engine) -> Optional[Booking]:
    """Gets the next booking

    Args:
        db_engine: the SQL engine to query from
    """
    with Session(db_engine) as session:
        current_timestamp = get_utc_now()
        statement = (
            select(Booking)
            .where(Booking.start_utc >= current_timestamp)
            .order_by(Booking.start_utc)
            .limit(1)
        )
        results = session.exec(statement)
        booking = results.first()
        if booking is None:
            return None

        # model validate to ensure timezone info is reloaded
        booking = Booking.model_validate(booking)
        session.expunge_all()
        return booking


def create_random_user(db_engine: Engine, user_id: str) -> User:
    """Creates a new random user for the given user id and returns it

    Args:
        db_engine: the SQL engine to save new job in
        user_id: the unique identifier of the user

    Returns:
        the created user

    Raises:
        ConflictError: booking conflicts with another user
    """
    user_data = NewUserInfo(
        name=f"User-{user_id}",
        email=f"{user_id}@{get_random_name()}.com",
        password=uuid_str(),
    )
    return create_user(db_engine, data=user_data)


def create_user(db_engine: Engine, data: NewUserInfo) -> User:
    """Creates a new user and returns it

    Args:
        db_engine: the SQL engine to save new job in
        data: the user info

    Returns:
        the created user

    Raises:
        ConflictError: booking conflicts with another user
    """
    with Session(db_engine) as session:
        user = User(**data.model_dump())
        # obfuscate the password
        user.password = hash_password(user.password)
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge_all()
        return user


def delete_users(db_engine: Engine, *filters: _Filter):
    """Deletes the users that match the given filters

    Args:
        db_engine: the SQL engine where the users are saved
        filters: the filters that the bookings should match
    """
    with Session(db_engine) as session:
        statement = delete(User).where(*filters)
        session.exec(statement)
        session.commit()


def get_user_profile(db_engine: Engine, user_id: str) -> UserProfile:
    """Returns the user profile of the user of the given user ID

    The user profile is a subset of the user details that
    are okay to be viewed by the public

    Args:
        db_engine: the SQL engine to query from
        user_id: the unique identifier of the user

    Returns:
        the user profile of the user

    Raises:
        ItemNotFoundError: user not found
    """
    user = get_user(db_engine, User.id == user_id)
    if user is None:
        raise ItemNotFoundError("user not found")

    return UserProfile.from_user(user)


def get_user(db_engine: Engine, *filters: _Filter) -> Optional[User]:
    """Retrieves the first user that matches the given filters

    Args:
        db_engine: the SQL engine to query from
        filters: the filters that the user should match

    Returns:
        the user or None if it does not exist
    """
    with Session(db_engine) as session:
        statement = select(User).where(*filters)
        results = session.exec(statement)
        user = results.first()
        session.expunge_all()
        return user


def get_many_user_profiles(
    db_engine: Engine,
    *filters: _Filter,
    skip: int = 0,
    limit: int | None = None,
    sort: tuple[_ColumnExpressionOrStrLabelArgument[Any]] = (),
) -> List[UserProfile]:
    """Retrieves the user profiles that match the given filters

    Args:
        db_engine: the SQL engine to query from
        filters: the filters that the bookings should match
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.
        sort: fields to sort by; default = (,)

    Returns:
        the list of bookings that match the given filters
    """
    with Session(db_engine) as session:
        statement = (
            select(User).where(*filters).limit(limit).offset(skip).order_by(*sort)
        )
        results = session.exec(statement)
        records = list(results.all())
        records = [UserProfile.from_user(v) for v in records]
        session.expunge_all()
        return records


def create_mss_jwt_token(
    user: "User",
    job_id: str,
    secret_key: str = settings.JWT_SECRET,
    ttl: float = settings.JWT_TTL,
    algorithm: str = "HS256",
) -> str:
    """Creates a JWT token that works for jobs submitted via MSS

    Note that extra claims `exp` and `sub` get overridden
    so don't set them directly

    Args:
        user: the user for whom the token is to be created
        job_id: the unique identifier fo the job from MSS
        secret_key: the key to use to sign the token
        ttl: the time to live for the token in seconds
        algorithm: the algorithm to use to sign the JWT

    Returns:
        the JWT token
    """
    exp = get_utc_now() + timedelta(seconds=ttl)
    payload = dict(job=job_id, exp=exp, sub=user.id)
    return jwt.encode(payload, secret_key, algorithm=algorithm)


def get_user_job_id_pair_from_token(
    token: str,
    secret_key: str = settings.JWT_SECRET,
    algorithm: str = "HS256",
) -> tuple[str, str]:
    """Gets the user_id and the job_id from the given JWT token

    Args:
        token: the JWT token of the user
        secret_key: the secret key for the JWT encoding/decoding
        algorithm: the algorithm used to encode the JWT

    Returns:
        the tuple of user_id, job_id from the token

    Raises:
        NotAuthenticatedError: not authenticated
    """
    try:
        payload = jwt.decode(token, secret_key, algorithms=[algorithm])
        user_id, job_id = payload["sub"], payload["job"]
        if user_id is None:
            raise ValueError("user_id is None")

        return user_id, job_id
    except (InvalidTokenError, KeyError, ValueError) as exp:
        logging.error(exp)
        raise NotAuthenticatedError("not authenticated")


def _get_overlapping_bookings(
    session: Session,
    start_utc: datetime,
    end_utc: datetime,
    *filters: _Filter,
    skip: int = 0,
    limit: Optional[int] = None,
    sort: tuple[_ColumnExpressionOrStrLabelArgument[Any]] = (),
) -> List[Booking]:
    """Gets the list of bookings that overlap in given period and fulfill the extra filters if any

    Args:
        session: the SQL session to query from
        start_utc: the start time of that period
        end_utc: the end time of that period
        filters: extra filters to match against
            e.g. belonging to particular user: Booking.user_id == user_id
        limit: maximum number of records to return; default is None.

    Returns:
        the list of bookings that overlap in the period
    """
    statement = (
        select(Booking)
        .where(Booking.start_utc < end_utc, Booking.end_utc > start_utc, *filters)
        .limit(limit)
        .offset(skip)
        .order_by(*sort)
    )
    results = session.exec(statement)
    return list(results.all())


def _validate_no_overlap(session: Session, booking_info: NewBookingInfo):
    """Validates that the booking_info does not overlap with another existing booking

    Args:
        session: the database session for accessing the saved bookings
        booking_info: the booking info under examination

    Raises:
        ConflictError: booking conflicts with another booking at {start_utc} to {end_utc}
    """
    try:
        overlap_record = _get_overlapping_bookings(
            session,
            start_utc=booking_info.start_utc,
            end_utc=booking_info.end_utc,
            limit=1,
        )[0]
        raise ConflictError(
            f"booking conflicts with another booking at {overlap_record.start_utc} to {overlap_record.end_utc}"
        )
    except IndexError:
        pass


def _validate_max_slots(session: Session, user_id: str, booking_info: NewBookingInfo):
    """Validates that the pre-existing bookings in the day this booking starts, are not above the limit

    Args:
        session: the database session for accessing the saved bookings
        user_id: the unique identifier of the user creating the booking
        booking_info: the booking info under examination

    Raises:
        MaxBookingsError: booking conflicts with another booking at {start_utc} to {end_utc}
    """
    day_start, day_end = get_day_range(booking_info.start_utc)
    user_bookings = _get_overlapping_bookings(
        session, day_start, day_end, Booking.user_id == user_id
    )
    if len(user_bookings) >= settings.MAX_SLOTS_PER_DAY:
        raise MaxBookingsError(
            f"you have exceeded the maximum {settings.MAX_SLOTS_PER_DAY} bookings per day for {day_start}-{day_end}"
        )
