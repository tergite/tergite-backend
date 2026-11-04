# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2023, 2024
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Utility functions for records"""
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Generic,
    List,
    Optional,
    Sequence,
    TypedDict,
    TypeVar,
)

from tests.utils.datetime import get_timestamp_str

T = TypeVar("T")


def order_by(
    data: List[Dict[str, Any]], field: str, is_descending: bool = False
) -> List[Dict[str, Any]]:
    """Orders the data by given field

    Args:
        data: the list of records to sort
        field: the field to order by
        is_descending: whether to sort in descending order

    Returns:
        the ordered list of records
    """
    return sorted(data, key=lambda x: x[field], reverse=is_descending)


def with_current_timestamps(
    data: List[dict],
    fields: Sequence[str] = ("updated_at",),
) -> List[dict]:
    """Gets data that has the current timestamp

    We update the fields passed with the corresponding timestamps

    Args:
        data: the list of dicts to attach timestamps to
        fields: the fields that should have the timestamps

    Returns:
        the data with timestamps
    """
    now = datetime.now(timezone.utc)
    return [
        {
            **item,
            **{field: get_timestamp_str(now) for field in fields},
        }
        for idx, item in enumerate(data)
    ]


def paginate(
    data: List[T], skip: int = 0, limit: Optional[int] = None
) -> "PaginatedList[T]":
    """Paginates the data basing on the skip and the limit params

    Args:
        skip: the number of records to skip
        limit: the maximum number of records to return

    Returns:
        list of the data sliced according to the pagination info
    """
    slice_limit = limit
    if isinstance(slice_limit, int):
        slice_limit += skip
    return {"skip": skip, "limit": limit, "data": data[skip:slice_limit]}


def drop_microseconds(timestamp: datetime) -> datetime:
    """Drops the microseconds on the timestamp

    Args:
        timestamp: the datetime which may or may not have microseconds

    Returns:
        the datetime but without microseconds
    """
    return timestamp.replace(microsecond=0)


def remove_dates(dynamic_properties: Dict[str, Any]) -> Dict[str, Any]:
    """A utility to do away with the dates in the dynamic properties

    Args:
        dynamic_properties: the properties to change

    Returns:
        the dynamic properties with "date" and "last_calibrated" replaced with "NOW-PLACEHOLDER"
    """
    now_placeholder = "NOW-PLACEHOLDER"
    qubits = dynamic_properties.get("qubits", [])
    couplers = dynamic_properties.get("couplers", [])
    resonators = dynamic_properties.get("resonators", [])
    return {
        **dynamic_properties,
        "last_calibrated": now_placeholder,
        "qubits": [
            {
                k: v if not isinstance(v, dict) else {**v, "date": now_placeholder}
                for k, v in qubit_conf.items()
            }
            for qubit_conf in qubits
        ],
        "couplers": [
            {
                k: v if not isinstance(v, dict) else {**v, "date": now_placeholder}
                for k, v in coupler_conf.items()
            }
            for coupler_conf in couplers
        ],
        "resonators": [
            {
                k: v if not isinstance(v, dict) else {**v, "date": now_placeholder}
                for k, v in resonator_conf.items()
            }
            for resonator_conf in resonators
        ],
    }


class PaginatedList(TypedDict, Generic[T]):
    """The type for paginated responses"""

    skip: int
    limit: Optional[int]
    data: List[T]
