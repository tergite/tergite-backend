# This code is part of Tergite
#
# (C) Martin Ahindura 2023
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
"""Module containing the schemas of the data storage/transfer objects"""

from datetime import datetime
from typing import List, Optional, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationInfo,
    computed_field,
    field_validator,
)
from sqlmodel import Field, Relationship, SQLModel

from utils.config import DeviceSettings
from utils.datetime import to_utc
from utils.strings import uuid_str


class MSSTokenClaims(BaseModel):
    """The model of the claims stored in special MSS JWT token given to users"""

    job_id: str
    user_id: str
    # whether the job's project may send pulse-level jobs, which MSS says on asking for
    # the token; an MSS that does not say lets none
    is_pulse_allowed: bool = False


class UserProfile(BaseModel):
    """Schema for a user's profile"""

    id: str
    name: str
    email: str
    is_admin: bool = False

    @classmethod
    def from_user(cls, user: "User") -> Self:
        """Generates a user profile object from a User object

        Args:
            user: the User object

        Returns:
            the user profile of the user
        """
        return cls(id=user.id, name=user.name, email=user.email, is_admin=user.is_admin)


class NewUserInfo(SQLModel, table=False):
    """Schema for creating new users"""

    id: str = Field(primary_key=True, index=True)
    name: str = Field()
    email: str = Field(unique=True, index=True)
    is_admin: bool = Field(default=False)


class User(NewUserInfo, table=True):
    """Schema for users"""

    bookings: List["Booking"] = Relationship(
        back_populates="user", passive_deletes="all"
    )


class NewBookingInfo(BaseModel):
    """Schema for creating new bookings"""

    start_utc: datetime
    end_utc: datetime

    @computed_field
    @property
    def total_duration(self) -> float:
        """The seconds the booking lasts, which the booking service checks against the
        device's limits"""
        return (self.end_utc - self.start_utc).total_seconds()

    @field_validator("start_utc", mode="after")
    @classmethod
    def validate_start_utc(cls, v: datetime):
        """Puts the start in UTC

        Args:
            v: the start

        Returns:
            the start in UTC
        """
        return to_utc(v)

    @field_validator("end_utc", mode="after")
    @classmethod
    def validate_end_utc(cls, v: datetime, info: ValidationInfo):
        """Puts the end in UTC, refusing one before the start

        Args:
            v: the end
            info: the fields validated so far, the start among them

        Returns:
            the end in UTC

        Raises:
            ValueError: end_utc ({end}) comes earlier than start_utc ({start})
        """
        v = to_utc(v)
        start_utc = info.data.get("start_utc")
        if start_utc and v < start_utc:
            raise ValueError(
                f"end_utc ({v}) comes earlier than start_utc ({start_utc})"
            )
        return v


class Booking(SQLModel, table=True):
    """Schema for booking

    Attributes:
        id: the unique identifier of the booking
        device_name: the device booked, of those that may share the database
        user_id: the unique identifier of the user associated with this booking
        user: the user associated with this booking
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=uuid_str, primary_key=True, index=True)
    device_name: str = Field(index=True)
    user_id: Optional[str] = Field(
        foreign_key="user.id", index=True, ondelete="SET NULL", nullable=True
    )
    start_utc: datetime = Field(index=True)
    end_utc: datetime = Field(index=True)

    user: Optional[User] = Relationship(
        back_populates="bookings", passive_deletes="all"
    )
    # its new booking info's, stored so that bookings sort by it
    total_duration: float

    @field_validator("start_utc", mode="after")
    @classmethod
    def validate_start_utc(cls, v: datetime):
        """Puts the start in UTC

        Args:
            v: the start

        Returns:
            the start in UTC
        """
        return to_utc(v)

    @field_validator("end_utc", mode="after")
    @classmethod
    def validate_end_utc(cls, v: datetime, info: ValidationInfo):
        """Puts the end in UTC, refusing one before the start

        Args:
            v: the end
            info: the fields validated so far, the start among them

        Returns:
            the end in UTC

        Raises:
            ValueError: end_utc ({end}) comes earlier than start_utc ({start})
        """
        v = to_utc(v)
        start_utc = info.data.get("start_utc")
        if start_utc and v < start_utc:
            raise ValueError(
                f"end_utc ({v}) comes earlier than start_utc ({start_utc})"
            )
        return v

    def is_active_at(self, now: datetime) -> bool:
        """Whether this booking must be running at the given time

        Args:
            now: the time to check at, in UTC

        Returns:
            True if the time is within the booking, ends included
        """
        return self.start_utc <= now <= self.end_utc

    def is_complete_at(self, now: datetime) -> bool:
        """Whether this booking must have completed by the given time

        Args:
            now: the time to check at, in UTC

        Returns:
            True if the booking ended before the time
        """
        return self.end_utc < now


class BookingsConfig(BaseModel):
    """Configurations for the booking service"""

    # Maximum time in seconds a booking is allowed to have
    max_time_slot_length: float

    # Minimum time in seconds a booking is allowed to have
    min_time_slot_length: float

    # Maximum number of bookings per day that a user can have
    max_slots_per_day: int

    # Maximum time in seconds that a booking can lie idle without a running job
    max_idle_time: int

    @classmethod
    def from_settings(cls, device_settings: DeviceSettings) -> Self:
        """Creates this configuration from the device's settings

        Args:
            device_settings: the settings of the device

        Returns:
            the bookings configuration of the device
        """
        return cls(
            max_time_slot_length=device_settings.max_time_slot_length,
            min_time_slot_length=device_settings.min_time_slot_length,
            max_slots_per_day=device_settings.max_slots_per_day,
            max_idle_time=device_settings.max_idle_time,
        )
