# This code is part of Tergite
#
# (C) Copyright David Wahlstedt 2023
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import ast
from dataclasses import asdict, dataclass
from enum import Enum, unique
from typing import Any, List, Optional, Tuple, TypeVar, Union

import redis

import settings

from .date_time import utc_now_iso
from .logging import get_logger
from .representation import to_string

# ============================================================================
# Types
# ============================================================================
Unit = str

TimeStamp = str  # in ISO 8601 UTC Z with microsecond precision

# Types for some measurement data

Frequency = float
Voltage = float

Hex = str  # type(hex(5))


# ============================================================================
# Logging initialization
# ============================================================================

logger = get_logger()

# ============================================================================
# Constants
# ============================================================================

TRANSACTION_MAX_RETRIES = 100

# =============================================================================
# Class definitions for storage data model
# =============================================================================

Counter = int

T = TypeVar("T")

# Since a class name is not allowed as type hint in its method
# declarations (see PEP 563), we introduce the following, for use in
# type hints:
_BackendProperty = Any


@unique
class PropertyType(str, Enum):
    DEVICE = "device"  # the string returned by str
    ENVIRONMENT = "environment"
    SETUP = "setup"

    def __str__(self) -> str:
        return str.__str__(self)


@dataclass
class BackendProperty:
    property_type: PropertyType

    name: str  # property name e.g. resonant_frequency
    value: Optional[T] = None  # the value of the property
    unit: Optional[Unit] = None

    component: Optional[str] = None  # "resonator", "qubit", "coupler"
    component_id: Optional[
        str
    ] = None  # component id, e.g. "1", "2", etc, or perhaps "q1", "q2", etc

    long_name: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    source: Optional[str] = None  # "measurement", "config", or "mock_up"

    def dict(self):
        """Returns the dict representation of this data class"""
        return asdict(self)

    def model_dump(self, *args, **kwargs):
        return self.dict()

    def write_value(self) -> bool:
        """Write the "value" field to Redis. Set the timestamp, and
        increase the counter. Return True if write succeeded, and
        False otherwise.
        """

        # Question: should we require that metadata is set before
        # allowing access to the value?

        value_key = self._create_redis_key("value")
        count_key = self._create_redis_key("count")
        timestamp_key = self._create_redis_key("timestamp")
        watch_keys = [value_key, count_key, timestamp_key]

        def set_fields(pipe):
            pipe.set(value_key, to_string(self.value))
            pipe.set(timestamp_key, to_string(utc_now_iso()))
            pipe.incrby(count_key, 1)

        results = _transaction(watch_keys, set_fields)
        return results is not None

    def write_metadata(self) -> bool:
        """Write all non-None fields to Redis except for the "value"
        field. Set the timestamp, but don't increase the counter.
        """
        metadata = [
            (field, value)
            for field, value in self.__dict__.items()
            if field in _included_fields - set(["value"]) and value is not None
        ]
        timestamp_key = self._create_redis_key("timestamp")

        def set_fields(pipe):
            for field, value in metadata:
                field_key = self._create_redis_key(field)
                pipe.set(field_key, to_string(value))
            pipe.set(timestamp_key, to_string(utc_now_iso()))

        results = _transaction([timestamp_key], set_fields)
        return results is not None

    def write(self) -> bool:
        """Write the whole record into Redis. Suitable for initialization."""
        success = self.write_metadata()
        success = success and self.write_value()
        return success

    @classmethod
    def read(
        cls,
        property_type: PropertyType,
        name: str,
        component: Optional[str] = None,
        component_id: Optional[str] = None,
    ) -> Optional[Tuple["BackendProperty", TimeStamp, Counter]]:
        """Get the backend property from Redis associated with kind,
        name, component, and component_id, when relevant, together with its
        metadata fields, plus Counter and TimeStamp (that are not
        class members)
        """

        value_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="value",
        )
        count_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="count",
        )
        timestamp_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="timestamp",
        )
        watch_keys = [value_key, count_key, timestamp_key]
        fields = list(_included_fields)

        def get_fields(pipe):
            for field in fields:
                field_key = create_redis_key(
                    property_type,
                    name,
                    component=component,
                    component_id=component_id,
                    field=field,
                )
                pipe.get(field_key)
            # these two are not class members:
            pipe.get(timestamp_key)
            pipe.get(count_key)

        results = _transaction(watch_keys, get_fields)
        if results is None:
            return

        field_entries = dict(
            {
                # all values v were created with to_string(v), therefore
                # ast.literal_eval (in _eval_redis_value) is safe here, unless something else has
                # gone seriously wrong
                (field, _eval_redis_value(value))
                for field, value in zip(fields + ["timestamp", "count"], results)
                if value is not None  # don't include keys absent in Redis
            }
        )
        # as long as field_entries is non-empty, we can instantiate
        if field_entries:
            # isolate the non class member fields
            timestamp = field_entries.pop("timestamp", None)
            count = field_entries.pop("count", None)
            return (
                cls(
                    property_type=property_type,
                    name=name,
                    component=component,
                    component_id=component_id,
                    **field_entries,
                ),
                timestamp,
                count,
            )
        else:
            # None of the fields had any Redis entry stored
            return

    @classmethod
    def read_value(
        cls,
        property_type: PropertyType,
        name: str,
        component: Optional[str] = None,
        component_id: Optional[str] = None,
    ) -> Optional[T]:
        """Return the value associated with kind, name, component and
        component_id (if relevant).
        Note: we don't use transactions here, but maybe we should?
        """
        value_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="value",
        )
        result = settings.REDIS_CONNECTION.get(value_key)
        return (
            _eval_redis_value(result)
            if result is not None and str(result).lower() != "nan"
            else None
        )

    def _create_redis_key(self, field: Optional[str] = None) -> str:
        return create_redis_key(
            self.property_type,
            self.name,
            component=self.component,
            component_id=self.component_id,
            field=field,
        )

    @classmethod
    def get_counter(
        cls,
        property_type: PropertyType,
        name: str,
        component: Optional[str] = None,
        component_id: Optional[str] = None,
    ) -> Optional[int]:
        # Gets the counter value of the property associated to the
        # given fields. If no counter value is set yet, but there is
        # metadata written, 0 is returned as counter value.
        #
        # Note: the counter value represents how many times the value
        # has been updated.
        #
        # Question: should we have this in a transaction?
        key_stem = create_redis_key(
            property_type, name, component=component, component_id=component_id
        )
        if next(settings.REDIS_CONNECTION.scan_iter(key_stem + "*"), None) is None:
            return None

        count_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="count",
        )
        result = settings.REDIS_CONNECTION.get(count_key)
        # if the metadata is set, but the counter is not yet
        # incremented, treat it as 0
        return int(result) if result is not None else 0

    @classmethod
    def reset_counter(
        cls,
        property_type: PropertyType,
        name: str,
        component: Optional[str] = None,
        component_id: Optional[str] = None,
    ) -> bool:
        """Reset the associated counter. Return True if successful,
        and False otherwise.
        """
        value_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="value",
        )
        count_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="count",
        )
        # should we set a new timestamp when the counter is reset?
        timestamp_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="timestamp",
        )
        watch_keys = [value_key, count_key, timestamp_key]

        def set_fields(pipe):
            pipe.set(count_key, 0)
            pipe.set(timestamp_key, to_string(utc_now_iso()))

        results = _transaction(watch_keys, set_fields)
        return results is not None

    @classmethod
    def get_timestamp(
        cls,
        property_type: PropertyType,
        name: str,
        component: Optional[str] = None,
        component_id: Optional[str] = None,
    ) -> Optional[int]:
        """Returns the timestamp of the property associated with kind,
        name, component, and component_id. If no property is associated,
        return None.
        """
        timestamp_key = create_redis_key(
            property_type,
            name,
            component=component,
            component_id=component_id,
            field="timestamp",
        )
        result = settings.REDIS_CONNECTION.get(timestamp_key)
        # The timestamp was stored by to_string as a quoted string, and
        # will now be turned into an unquoted string
        return (
            _eval_redis_value(result)
            if result is not None and str(result).lower() != "nan"
            else None
        )

    @classmethod
    def delete_property(
        cls,
        property_type: PropertyType,
        name: str,
        component: Optional[str] = None,
        component_id: Optional[str] = None,
    ):
        """Deletes all Redis the key-value bindings associated with
        the identified property.
        """
        key_stem = create_redis_key(
            property_type, name, component=component, component_id=component_id
        )
        for key in settings.REDIS_CONNECTION.scan_iter(key_stem + "*"):
            settings.REDIS_CONNECTION.delete(key)


# The fields of BackendProperty as a set
_all_fields = set(BackendProperty.__dataclass_fields__.keys())

# These fields are part of the key (when present), so they don't need
# to be stored as fields in Redis
_excluded_fields = set(["property_type", "name", "component", "component_id"])

# Only these are stored in Redis, the others are part of the key
_included_fields = _all_fields - _excluded_fields


# =============================================================================
# Local helper functions
# =============================================================================


def _transaction(watch_keys: List[str], command: callable) -> Optional[list]:
    """Perform 'command', watching 'watch_keys'. If any of the watch
    keys are modified in Redis during execution of 'command', the
    command will be discarded and re-attempted, at most
    TRANSACTION_MAX_RETRIES times. If it doesn't succeed then, None is
    returned. If successful, a list of results of the pipeline
    operation results is returned.
    """
    with settings.REDIS_CONNECTION.pipeline() as pipe:
        n_retries = 0
        while True:
            try:
                pipe.watch(*watch_keys)
                pipe.multi()
                command(pipe)  # this does what should be inside the transaction
                results = pipe.execute()
                return results
            except redis.WatchError as e:
                n_retries += 1
                logger.warning(
                    f"{e}, at least one of {' ,'.join(watch_keys)} were changed by "
                    f"another process: retrying, {n_retries=}"
                )
                logger.warning("(caller location)", stacklevel=2)
                if n_retries > TRANSACTION_MAX_RETRIES:
                    logger.warning(
                        f"Transaction failed, since max number of allowed "
                        f"retries ({TRANSACTION_MAX_RETRIES}) were exceeded."
                    )
                    return


# =============================================================================
# Public helper functions
# =============================================================================


def create_redis_key(
    property_type: PropertyType,
    name: str,
    component: Optional[str] = None,
    component_id: Optional[str] = None,
    field: Optional[str] = None,
) -> str:
    """Creates a Redis key from the given arguments, identifying a
    backend property. If 'field' is omitted, the key obtained is a
    common prefix for all keys associated with the property.

    Note: made public in order to allow other functions(e.g. in
    calibration framework) to use keys that may contain this key,
    without relying on how it actually looks.
    """
    opt_component = f":{component}" if component else ""
    opt_component_id = f":{component_id}" if component_id != None else ""
    opt_field = f":{field}" if field else ""
    # f"{property_type}" == str(property_type), so we get the right string value
    return f"{property_type}{opt_component}{opt_component_id}:{name}{opt_field}"


"""Component helpers"""


def set_component_property(
    component: str,
    name: str,
    component_id: str,
    **fields,
):
    """Set the component device property identified by
    property_type, name, component, and component_id, to the bindings given
    in fields.
    """
    property_type = PropertyType.DEVICE
    p = BackendProperty(
        property_type, name, component=component, component_id=component_id, **fields
    )
    p.write_metadata()
    p.write_value()


def get_component_property(
    component: str,
    name: str,
    component_id: str,
):
    property_type = PropertyType.DEVICE
    return BackendProperty.read(
        property_type, name, component=component, component_id=component_id
    )


def get_component_value(
    component: str,
    name: str,
    component_id: str,
) -> Optional[T]:
    property_type = PropertyType.DEVICE
    return BackendProperty.read_value(
        property_type, name, component=component, component_id=component_id
    )


"""Resonator helpers"""


def set_resonator_property(name: str, component_id: str, **fields):
    """Write given fields into Redis for resonator property identified
    by the given arguments.
    """
    set_component_property("resonator", name, component_id, **fields)


def get_resonator_property(
    name: str, component_id: str
) -> Optional[Tuple[_BackendProperty, TimeStamp, Counter]]:
    """Get all fields associated with the resonator property
    identified by the given arguments.
    """
    return get_component_property("resonator", name, component_id)


def set_resonator_value(name: str, component_id: str, value: T):
    """Write given value into Redis for resonator property identified
    by the given arguments.
    """
    set_component_property("resonator", name, component_id, value=value)


def get_resonator_value(name: str, component_id: str) -> Optional[T]:
    """Get the value associated with the resonator property
    identified by the given arguments.
    """
    return get_component_value("resonator", name, component_id)


def _eval_redis_value(value: Union[bytes, str]) -> Any:
    """Evaluates the value from redis

    Args:
        value: the value to evaluate

    Returns:
        the evaluated value
    """
    value_str = value if isinstance(value, str) else value.decode("utf-8")
    return ast.literal_eval(value_str)
