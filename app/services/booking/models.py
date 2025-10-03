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
from typing import Any, List, Optional, Self

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator
from sqlmodel import Field, Relationship, SQLModel

from app.utils.datetime import get_relative_time, get_utc_now, to_utc
from app.utils.strings import uuid_str


class MSSTokenClaims(BaseModel):
    """The model of the claims stored in special MSS JWT token given to users"""

    __primary_key_fields__ = ("job_id", "user_id")

    job_id: str
    user_id: str


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
    password: str = Field()
    is_admin: bool = Field(default=False)


class User(NewUserInfo, table=True):
    """Schema for users"""

    bookings: List["Booking"] = Relationship(
        back_populates="user", passive_deletes="all"
    )


class NewBookingInfo(BaseModel):
    """Schema for creating new bookings"""

    # we validate on assignment so that total_duration always in sync
    # with the start_utc and end_utc
    model_config = ConfigDict(validate_assignment=True)

    start_utc: datetime
    end_utc: datetime

    @field_validator("start_utc", mode="after")
    @classmethod
    def validate_start_utc(cls, v: datetime):
        """Validate start_utc"""
        v = to_utc(v)
        if v < get_relative_time(seconds=-1):
            raise ValueError(f"start_utc ({v}) is in the past")
        return v

    @field_validator("end_utc", mode="after")
    @classmethod
    def validate_end_utc(cls, v: datetime, info: ValidationInfo):
        """Validate end_utc"""
        v = to_utc(v)
        start_utc = info.data.get("start_utc")
        if start_utc and v < start_utc:
            raise ValueError(
                f"end_utc ({v}) comes earlier than start_utc ({start_utc})"
            )
        if v < get_relative_time(seconds=-1):
            raise ValueError(f"end_utc ({v}) is in the past")
        return v


class Booking(SQLModel, table=True):
    """Schema for booking

    Attributes:
        id: the unique identifier of the booking
        user_id: the unique identifier of the user associated with this booking
        user: the user associated with this booking
        start_event_id: the rq job id of the special event triggered at the start of this booking
        end_event_id: the rq job id of the special event triggered at the end of this booking
        idle_timer_id: the rq job id for the job that tracks the idleness of this booking
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=uuid_str, primary_key=True, index=True)
    user_id: Optional[str] = Field(
        foreign_key="user.id", index=True, ondelete="SET NULL", nullable=True
    )
    start_utc: datetime = Field(index=True)
    end_utc: datetime = Field(index=True)

    # optional: event_ids are overwritten by the validators
    start_event_id: str = Field(default="")
    end_event_id: str = Field(default="")
    idle_timer_id: str = Field(default="")
    user: Optional[User] = Relationship(
        back_populates="bookings", passive_deletes="all"
    )
    # optional: total_duration is overwritten by field validator
    total_duration: float = Field(default=0)

    @field_validator("start_event_id", mode="after")
    @classmethod
    def generate_start_event_id(cls, v: Any, info: ValidationInfo):
        """Generates the job ID for the start event"""
        return f"{info.data['id']}_start_event"

    @field_validator("end_event_id", mode="after")
    @classmethod
    def generate_end_event_id(cls, v: Any, info: ValidationInfo):
        """Generates the job ID for the end event"""
        return f"{info.data['id']}_end_event"

    @field_validator("idle_timer_id", mode="after")
    @classmethod
    def generate_idle_timer_id(cls, v: Any, info: ValidationInfo):
        """Generates the job ID for the idleness timer"""
        return f"{info.data['id']}_idle_timer"

    @field_validator("start_utc", mode="after")
    @classmethod
    def validate_start_utc(cls, v: datetime):
        """Validate start_utc"""
        return to_utc(v)

    @field_validator("end_utc", mode="after")
    @classmethod
    def validate_end_utc(cls, v: datetime, info: ValidationInfo):
        """Validate end_utc"""
        v = to_utc(v)
        start_utc = info.data.get("start_utc")
        if start_utc and v < start_utc:
            raise ValueError(
                f"end_utc ({v}) comes earlier than start_utc ({start_utc})"
            )
        return v

    @field_validator("total_duration", mode="after")
    @classmethod
    def validate_total_duration(cls, v: Any, info: ValidationInfo):
        """Generate and validate the total duration of this booking"""
        from settings import MAX_TIME_SLOT_LENGTH, MIN_TIME_SLOT_LENGTH

        try:
            duration = (info.data["end_utc"] - info.data["start_utc"]).total_seconds()

            if duration > MAX_TIME_SLOT_LENGTH:
                raise ValueError(
                    f"duration is beyond the limit of {MAX_TIME_SLOT_LENGTH} seconds"
                )
            if duration < MIN_TIME_SLOT_LENGTH:
                raise ValueError(
                    f"duration is too short compared to the minimum of {MIN_TIME_SLOT_LENGTH} seconds"
                )
            return duration
        except KeyError:
            return 0

    @property
    def is_active(self) -> bool:
        """Whether this booking must be running or not"""
        now = get_utc_now()
        return self.start_utc <= now <= self.end_utc

    @property
    def is_complete(self) -> bool:
        """Whether this booking must have completed or not"""
        now = get_utc_now()
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
    def from_settings(cls) -> "BookingsConfig":
        """Creates this configuration from the settings"""
        import settings

        return cls(
            max_time_slot_length=settings.MAX_TIME_SLOT_LENGTH,
            min_time_slot_length=settings.MIN_TIME_SLOT_LENGTH,
            max_slots_per_day=settings.MAX_SLOTS_PER_DAY,
            max_idle_time=settings.MAX_IDLE_TIME,
        )
