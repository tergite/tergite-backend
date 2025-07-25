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
"""Module containing the source code for storing data"""
from contextlib import suppress
from functools import cached_property
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from pydantic import BaseModel
from redis import Redis
from redis.client import Pipeline
from typing_extensions import Literal

from app.utils.exc import BaseBccException
from app.utils.model import create_partial_schema

IncEx = Union[Set[str], Set[int], Dict[int, Any], Dict[str, Any], None]
_KEY_SEPARATOR = "@@@"
_IDX_SEPARATOR = "::"


class Schema(BaseModel):
    """The base class for all schemas to be used in collections"""

    __primary_key_fields__: Tuple[str, ...] = ("id",)
    __index_fields__: Tuple[str, ...] = ()

    @classmethod
    def construct_redis_key(
        cls, __key: Union[str, Tuple[Any, ...], Dict[str, Any]]
    ) -> str:
        """Gets the redis key given the primary key values

        Args:
            __key: Can be the value of the primary key field,
                or the values of the primary fields in the right order,
                or a dictionary of the primary fields and their values

        Returns:
            the redis key string

        Raises:
            KeyError: some primary key fields were not set
        """
        keys = __key
        if isinstance(__key, str):
            keys = (__key,)
        if isinstance(__key, dict):
            try:
                keys = [__key[k] for k in cls.__primary_key_fields__]
            except KeyError as exp:
                raise KeyError(f"some primary key fields were not set: {exp}")

        return _KEY_SEPARATOR.join(keys)

    def model_dump(
        self,
        *,
        mode: Union[Literal["json", "python"], str] = "python",
        include: IncEx = None,
        exclude: IncEx = None,
        context: Any | None = None,
        by_alias: bool = False,
        exclude_unset: bool = True,
        exclude_defaults: bool = False,
        exclude_none: bool = True,
        round_trip: bool = False,
        warnings: bool | Literal["none", "warn", "error"] = True,
        fallback: Callable[[Any], Any] | None = None,
        serialize_as_any: bool = False,
    ) -> dict[str, Any]:
        return super().model_dump(
            mode=mode,
            include=include,
            exclude=exclude,
            by_alias=by_alias,
            context=context,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            round_trip=round_trip,
            warnings=warnings,
            fallback=fallback,
            serialize_as_any=serialize_as_any,
        )


T = TypeVar("T", bound=Schema)


class Collection(Generic[T]):
    """A synchronous collection of items of similar types"""

    def __init__(
        self,
        connection: Redis,
        schema: Type[T],
        partial_schema: Optional[Type[T]] = None,
    ):
        """
        Args:
            connection: the redis connection to which this collection is attached
            schema: the schema for all items in the collection
            partial_schema: the schema for partial updates; default: None
        """
        if partial_schema is None:
            partial_schema = create_partial_schema(
                f"Partial{schema.__qualname__}", original=schema
            )

        self._connection = connection
        self._schema = schema
        self._partial_schema = partial_schema
        self._hashmap_name = f"{schema.__module__}.{schema.__qualname__}".lower()
        self._index_prefix = f"__index__{self._hashmap_name}_"

    @cached_property
    def _index_fields(self) -> Tuple[str, ...]:
        """The index fields for the schema in this store

        This combines both the primary key fields and the index fields
        """
        index_map = {k: None for k in self._schema.__primary_key_fields__}
        index_map.update({k: None for k in self._schema.__index_fields__})
        return tuple(index_map)

    @cached_property
    def _index_field_set(self) -> Set[str]:
        """Set of all fields that are indexed"""
        return set(self._index_fields)

    def exists(self, key: Union[str, Tuple[Any, ...], Dict[str, Any]]) -> bool:
        """Checks if an item with the same primary keys exists

        The key can be the raw key used in redis,
        or the values of the primary fields in the right order,
        or a dictionary of the primary fields and their values

        Args:
            key: the unique key that identifies that item

        Returns:
            True if the collection contains an item with the same key

        Raises:
            KeyError: some primary key fields were not set
        """
        redis_key = self._schema.construct_redis_key(key)
        return self._connection.hexists(self._hashmap_name, redis_key)

    def get_one(self, key: Union[str, Tuple[Any, ...], Dict[str, Any]]) -> T:
        """Get one item by key

        The key can be the raw key used in redis,
        or the values of the primary fields in the right order,
        or a dictionary of the primary fields and their values

        Args:
            key: the unique key that identifies that item

        Returns:
            the item identified by the given key in the hash

        Raises:
            ItemNotFoundError: '{key}' not found
            ValidationError: item does not match the given schema
            KeyError: some primary key fields were not set
        """
        redis_key = self._schema.construct_redis_key(key)

        data = self._connection.hget(self._hashmap_name, redis_key)
        if data is None:
            raise ItemNotFoundError(f"'{key}' not found")

        return self._schema.model_validate_json(data)

    def get_all(self, desc: bool = False) -> List[T]:
        """Get all items in this collection, sorted by key

        Args:
            desc: if the items should come in descending order of keys

        Returns:
            the list of items in this collection

        Raises:
            ValidationError: item does not match the given schema
        """
        raw_data = self._connection.hgetall(self._hashmap_name)
        data = [self._schema.model_validate_json(item) for item in raw_data.values()]
        return sorted(data, key=lambda v: _get_redis_key(self._schema, v), reverse=desc)

    def find_by_keys(
        self, *keys: Union[str, Tuple[Any, ...], Dict[str, Any]]
    ) -> List[T]:
        """Get the items with the given keys in this collection

        Args:
            keys: the unique identifiers of the items to return

        Returns:
            the list of items that have the given keys

        Raises:
            ValidationError: item does not match the given schema
        """
        redis_keys = [self._schema.construct_redis_key(key) for key in keys]
        raw_data = self._connection.hmget(self._hashmap_name, redis_keys)
        return [
            self._schema.model_validate_json(item)
            for item in raw_data
            if item is not None
        ]

    def find_by_index(self, filters: Union[Dict[str, Any], T]) -> List[T]:
        """Get the items with the given values in the given indexed fields

        Args:
            filters: the dict of key and value that should be matched

        Returns:
            the list of items that match

        Raises:
            ValidationError: item does not match the given schema
            ValueError: the fields {non_indexed_fields} are not indexed
            ValidationError: if filters does not satisfy the partial schema of the collection
        """
        parsed_filters = self._partial_schema.model_validate(filters)
        filters = parsed_filters.model_dump(exclude_unset=True, exclude_defaults=True)

        non_indexed_fields = set(filters) - self._index_field_set
        if len(non_indexed_fields) > 0:
            raise ValueError(f"the fields {non_indexed_fields} are not indexed")

        index_keys = self._get_index_keys_from_dict(filters)

        matched_keys = self._connection.sinter(index_keys)
        if len(matched_keys) == 0:
            return []

        raw_data = self._connection.hmget(self._hashmap_name, matched_keys)
        return [self._schema.model_validate_json(item) for item in raw_data]

    def insert(self, payload: T):
        """Inserts the item identified by the primary key, replacing it if it exists

        Args:
            payload: the item to update or insert

        Returns:
            the current item in the collection

        Raises:
            ValidationError: payload does not satisfy the schema of the collection
            AttributeError: some primary key fields were not set
        """
        key = _get_redis_key(self._schema, payload)

        self._schema.model_validate(payload, from_attributes=True)
        data = payload.model_dump_json()
        pipe = self._connection.pipeline()

        # insert the record and its indexes
        pipe.hset(self._hashmap_name, key, data)
        self._insert_indexes(pipe, payload)
        pipe.execute()

        return payload

    def update(
        self,
        key: Union[str, Tuple[Any, ...], Dict[str, Any]],
        updates: Union[Dict[str, Any], T],
    ) -> T:
        """Updates the item identified by the primary key with the new updates

        Args:
            key: the unique key that identifies that item
            updates: the new fields and values to add.

        Returns:
            the item after updating

        Raises:
            ValidationError: updates does not satisfy the partial schema of the collection
            ItemNotFound: '{key}' not found
        """
        parsed_updates = self._partial_schema.model_validate(updates)
        updates_dict = parsed_updates.model_dump(
            exclude_unset=True, exclude_defaults=True
        )

        old_item = self.get_one(key)
        new_props = {**old_item.model_dump(), **updates_dict}
        updated_item = self._schema(**new_props)
        redis_key = self._schema.construct_redis_key(key)
        pipe = self._connection.pipeline()

        # update record and indexes
        pipe.hset(self._hashmap_name, redis_key, updated_item.model_dump_json())
        self._update_indexes(pipe, original=old_item, updates=updates_dict)
        pipe.execute()
        return updated_item

    def delete_many(self, keys: Sequence[Union[str, Tuple[Any, ...], Dict[str, Any]]]):
        """Get many items by their keys

        The keys can be the tuples of the raw keys used in redis,
        or sequence of tuples of the values of the primary fields in the right order,
        or sequence of dictionaries of the primary fields and their values

        Args:
            keys: the unique keys that identify that items

        Raises:
            ValidationError: item does not match the given schema
            KeyError: some primary key fields were not set
        """
        old_items = self.find_by_keys(*keys)
        redis_keys = [self._schema.construct_redis_key(key) for key in keys]

        pipe = self._connection.pipeline()
        pipe.hdel(self._hashmap_name, *redis_keys)

        # delete the index entries for the deleted items
        self._delete_indexes(pipe, old_items)
        pipe.execute()

    def clear(self):
        """Clears all items in this collection"""
        # delete hashmap and all associated indexes
        all_index_keys = self._get_all_index_keys()
        pipe = self._connection.pipeline()
        pipe.delete(self._hashmap_name, *all_index_keys)
        pipe.execute()

    def _insert_indexes(self, conn: Union[Pipeline, Redis], record: T):
        """Inserts the index entries for the given record

        Args:
            conn: the redis connection or pipeline
            record: the item whose indexes are to be inserted
        """
        record_key = _get_redis_key(self._schema, record)
        idx_keys = self._get_index_keys(record)

        for idx_key in idx_keys:
            conn.sadd(idx_key, record_key)

    def _delete_indexes(self, conn: Union[Pipeline, Redis], records: List[T]):
        """Deletes the index entries associated with the given records

        Args:
            conn: the redis connection or pipeline
            records: the items whose index entries are to be removed
        """
        for item in records:
            idx_keys = self._get_index_keys(item)
            key = _get_redis_key(self._schema, item)

            for idx_key in idx_keys:
                conn.srem(idx_key, key)

    def _update_indexes(
        self, conn: Union[Pipeline, Redis], original: T, updates: Dict[str, Any]
    ):
        """Updates the index entries of the old item with updates from partial_update

        Args:
            conn: the redis connection or pipeline
            original: the original record
            updates: the partial updates potentially containing new index entries
        """
        record_key = _get_redis_key(self._schema, original)

        old_idx_keys = self._get_index_keys(original)
        new_idx_keys = self._get_index_keys_from_dict(updates)
        old_idx_keys_mapper = {_get_index_prop(k): k for k in old_idx_keys}

        for new_idx_key in new_idx_keys:
            prop = _get_index_prop(new_idx_key)
            old_idx_key = old_idx_keys_mapper[prop]
            # update = remove (old) + insert (new)
            conn.srem(old_idx_key, record_key)
            conn.sadd(new_idx_key, record_key)

    def _get_all_index_keys(self) -> List[str]:
        """Gets all the index keys for the collection

        Returns:
            list of all the raw index keys associated with this collection
        """
        cursor = 0
        index_pattern = f"{self._index_prefix}{_IDX_SEPARATOR}*"
        all_index_keys = []

        while True:
            cursor, idx_keys = self._connection.scan(
                cursor, match=index_pattern, _type="SET"
            )
            all_index_keys.extend(idx_keys)
            if cursor == 0:
                break

        return all_index_keys

    def _get_index_keys(self, item: T) -> List[str]:
        """Gets the keys for the index of this schema

        Args:
            item: the item from which to extract the key

        Returns:
            the list of redis set keys corresponding to the given item

        Raises:
            AttributeError: some index key fields were not set
        """
        index_keys = []
        for field in self._index_fields:
            try:
                value = getattr(item, field)
                index_keys.append(
                    f"{self._index_prefix}{_IDX_SEPARATOR}{field}{_IDX_SEPARATOR}{value}"
                )
            except AttributeError as exp:
                raise AttributeError(f"some index key fields were not set: {exp}")

        return index_keys

    def _get_index_keys_from_dict(self, item: Dict[str, Any]) -> List[str]:
        """Gets the keys for the index of this schema

        Args:
            item: the item as a dict from which to extract the key

        Returns:
            the list of redis set keys corresponding to the given item
        """
        index_keys = []
        for field in self._index_fields:
            with suppress(KeyError):
                index_keys.append(
                    f"{self._index_prefix}{_IDX_SEPARATOR}{field}{_IDX_SEPARATOR}{item[field]}"
                )

        return index_keys


class ItemNotFoundError(BaseBccException):
    """Exception when item is not found"""


def _get_redis_key(schema: Type[Schema], item: Any) -> str:
    """Gets the redis key for this item

    Args:
        schema: the schema under consideration
        item: the item from which to extract the key

    Returns:
        the redis key string corresponding to the given item

    Raises:
        AttributeError: some primary key fields were not set
    """
    try:
        keys = tuple(getattr(item, field) for field in schema.__primary_key_fields__)
        return _KEY_SEPARATOR.join(keys)
    except AttributeError as exp:
        raise AttributeError(f"some primary key fields were not set: {exp}")


def _get_index_prop(index_key: str) -> str:
    """Gets the index property name from the index key

    e.g. from "_index_prefix_module.schema::prop::value"
    to "_index_prefix_module.schema::prop"

    Args:
        index_key: the full key of the index including the property value

    Returns:
        the part of the key excluding the property value
    """
    return index_key.rsplit(_IDX_SEPARATOR, 1)[0]
