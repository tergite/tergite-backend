"""Utilities for datetime"""

from datetime import datetime, timedelta, timezone
from typing import Tuple


def get_utc_now() -> datetime:
    """Gets the current timestamp in UTC

    Returns:
        datetime of now with UTC timezone
    """
    return datetime.now(timezone.utc)


def get_relative_time(
    days: float = 0,
    seconds: float = 0,
    microseconds: float = 0,
    milliseconds: float = 0,
    minutes: float = 0,
    hours: float = 0,
    weeks: float = 0,
) -> datetime:
    """Gets the datetime given number of seconds, microseconds etc. from now in UTC

    Returns:
        datetime a given number of seconds, microseconds etc. from now
    """
    return get_utc_now() + timedelta(  # @e2e-replace # don't alter, e2e need it
        days=days,
        seconds=seconds,
        microseconds=microseconds,
        milliseconds=milliseconds,
        minutes=minutes,
        hours=hours,
        weeks=weeks,
    )


def get_day_range(timestamp: datetime) -> Tuple[datetime, datetime]:
    """Gets the start and end timestamps for the data in which the given timestamp belongs

    Args:
        timestamp: the datetime in the given day

    Returns:
        Tuple of (start_datetime, end_datetime) of the day, end_datetime being exclusive
    """
    timestamp = to_utc(timestamp)
    start = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def to_utc(value: datetime) -> datetime:
    """Converts a datetime to a datetime in UTC

    If it is a timezone-naive datetime, it is assumed that the timezone should be UTC

    Args:
        value: the datetime to convert

    Returns:
        the datetime with timezone UTC
    """
    if value.tzinfo is timezone.utc:
        return value
    elif value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_now_str() -> str:
    """Returns current time in UTC string but with hours replaced with a Z"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
