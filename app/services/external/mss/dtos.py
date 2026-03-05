# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2025, 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from enum import Enum
from typing import Dict, Type, Union

from pydantic import BaseModel, Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from app.libs.device_parameters import Device, DeviceCalibration
from app.libs.queues.dtos import Job
from app.utils.api import GeneralMessage
from app.utils.strings import uuid_str


class DeviceEventName(str, Enum):
    INITIALIZED = "initialized"
    RECALIBRATED = "recalibrated"
    JOB_UPDATED = "job_updated"


type DeviceEventData = Union[Device, DeviceCalibration, Job]
"""Data type for the data attached to device events"""


class EventResponse(GeneralMessage):
    """Response to an event"""

    id: str


class DeviceEvent(BaseModel):
    """The schema for device events"""

    __status_data_map__: Dict[DeviceEventName, Type[DeviceEventData]] = {
        DeviceEventName.INITIALIZED: Device,
        DeviceEventName.RECALIBRATED: DeviceCalibration,
        DeviceEventName.JOB_UPDATED: Job,
    }

    id: str = Field(default_factory=uuid_str)
    name: DeviceEventName
    data: DeviceEventData

    @field_validator("data", mode="after")
    @classmethod
    def validate_data(
        cls, value: DeviceEventData, info: ValidationInfo
    ) -> DeviceEventData:
        """Validates the data depending on the name type"""
        expected_data_cls = cls.__status_data_map__[info.data["name"]]
        if not isinstance(value, expected_data_cls):
            raise ValueError(
                f"data must be of type {expected_data_cls.__name__}, was {type(value)}"
            )

        return value
