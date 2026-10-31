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
from datetime import datetime
from enum import Enum
from typing import Dict, Type, Union

from pydantic import BaseModel, Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from libs.device_parameters import Device, DeviceCalibration
from libs.queues.dtos import Job
from utils.messages import GeneralMessage
from utils.strings import uuid_str


class DeviceEventName(str, Enum):
    """The kinds of events the device sends MSS"""

    INITIALIZED = "initialized"
    RECALIBRATED = "recalibrated"
    JOB_UPDATED = "job_updated"
    SWITCHED_OFF = "switched_off"
    SWITCHED_ON = "switched_on"


class DeviceSwitchData(BaseModel):
    """The data of the event that the device was switched on or off"""

    date: datetime


type DeviceEventData = Union[Device, DeviceCalibration, Job, DeviceSwitchData]
"""Data type for the data attached to device events"""


class EventResponse(GeneralMessage):
    """Response to an event"""

    id: str


# the type of each event's data
_EVENT_DATA_TYPES: Dict[DeviceEventName, Type[DeviceEventData]] = {
    DeviceEventName.INITIALIZED: Device,
    DeviceEventName.RECALIBRATED: DeviceCalibration,
    DeviceEventName.JOB_UPDATED: Job,
    DeviceEventName.SWITCHED_OFF: DeviceSwitchData,
    DeviceEventName.SWITCHED_ON: DeviceSwitchData,
}


class DeviceEvent(BaseModel):
    """The schema for device events"""

    id: str = Field(default_factory=uuid_str)
    name: DeviceEventName
    data: DeviceEventData

    @field_validator("data", mode="after")
    @classmethod
    def validate_data(
        cls, value: DeviceEventData, info: ValidationInfo
    ) -> DeviceEventData:
        """Validates the data depending on the name type

        Args:
            value: the event's data
            info: the fields validated so far, the event's name among them

        Returns:
            the data

        Raises:
            ValueError: data must be of type {expected type}, was {actual type}
        """
        expected_data_cls = _EVENT_DATA_TYPES[info.data["name"]]
        if not isinstance(value, expected_data_cls):
            raise ValueError(
                f"data must be of type {expected_data_cls.__name__}, was {type(value)}"
            )

        return value
