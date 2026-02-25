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
"""Module containing tests for the store library"""
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Type, Union
from uuid import uuid4

import pytest
from pydantic import Field, ValidationError
from redis import Redis

from app.libs.queues.dtos import JobStatus
from app.tests.utils.datetime import get_current_timestamp_str
from app.tests.utils.records import with_current_timestamps
from app.utils.model import create_partial_schema
from app.utils.redis_store import Collection, ItemNotFoundError, Schema


class Credentials(Schema):
    """The model used for authenticating jobs"""

    __primary_key_fields__ = ("job_id", "app_token")

    job_id: str
    app_token: str


class AuthLog(Credentials):

    __index_fields__ = ("status",)
    status: JobStatus
    created_at: str = Field(default_factory=get_current_timestamp_str)
    updated_at: str = Field(default_factory=get_current_timestamp_str)


# derived models
PartialAuthLog = create_partial_schema(
    "PartialAuthLog", original=AuthLog, exclude=("created_at",)
)


_AUTH_LOG_LIST = [
    {"job_id": "foo", "app_token": "bar"},
    {"job_id": "foot", "app_token": "bar", "status": "executing"},
    {"job_id": "feet", "app_token": "ben", "status": "failed"},
    {"job_id": "fee", "app_token": "barracuda"},
    {"job_id": "billings", "app_token": "zealot"},
]
_DELETE_SLICES = [
    (0, -1),
    (1, 4),
    (2, 3),
]

_INDEX_PREFIXES = {
    "job_id": f"__index__{__name__}.authlog_::job_id::",
    "app_token": f"__index__{__name__}.authlog_::app_token::",
    "status": f"__index__{__name__}.authlog_::status::",
}


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_update_by_single_key(redis_client, payload, freezer):
    """Calling update() with a raw redis key updates the item if it exists already"""
    auth_logs = Collection(redis_client, schema=AuthLog, partial_schema=PartialAuthLog)
    payload = with_current_timestamps([payload], fields=["updated_at", "created_at"])[0]

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)
    key = _get_redis_key(original_item)

    new_update = {
        "status": "successful",
        "job_id": payload["job_id"],
        "app_token": payload["app_token"],
    }
    new_item = auth_logs.update(key, PartialAuthLog(**new_update))
    new_item_in_db = _get_redis_value(redis_client, original_item)

    assert original_item_in_db == original_item.model_dump_json()
    assert new_item_in_db == new_item.model_dump_json()
    assert new_item.model_dump() == {
        **original_item.model_dump(),
        "status": "successful",
    }


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_update_ttl(redis_client, payload):
    """Calling update(ttl=ttl) updates the item and its ttl if it exists already"""
    auth_logs = Collection(
        redis_client, schema=AuthLog, partial_schema=PartialAuthLog, cleanup_interval=1
    )
    payload = with_current_timestamps([payload], fields=["updated_at", "created_at"])[0]

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)
    key = _get_redis_key(original_item)

    ttl = 1
    new_update = {
        "status": "successful",
        "job_id": payload["job_id"],
        "app_token": payload["app_token"],
    }
    new_item = auth_logs.update(key, PartialAuthLog(**new_update), ttl=ttl)
    new_item_in_db = _get_redis_value(redis_client, original_item)

    time.sleep(ttl)

    with pytest.raises(ItemNotFoundError):
        auth_logs.get_one(key)

    # a mutation to force cleanup
    auth_logs.delete_many(())

    new_item_in_db_after_ttl = _get_redis_value(redis_client, original_item)

    assert original_item_in_db == original_item.model_dump_json()
    assert new_item_in_db == new_item.model_dump_json()
    assert new_item.model_dump() == {
        **original_item.model_dump(),
        "status": "successful",
    }
    assert new_item_in_db_after_ttl is None


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_dict_update_by_single_key(redis_client, payload, freezer):
    """Calling update() with a raw redis key, and dict updates, changes the item if it exists already"""
    auth_logs = Collection(redis_client, schema=AuthLog)
    payload = with_current_timestamps([payload], fields=["updated_at", "created_at"])[0]

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)
    key = _get_redis_key(original_item)

    new_update = {
        "status": "successful",
        "job_id": payload["job_id"],
        "app_token": payload["app_token"],
    }
    new_item = auth_logs.update(key, new_update)
    new_item_in_db = _get_redis_value(redis_client, original_item)

    assert original_item_in_db == original_item.model_dump_json()
    assert new_item_in_db == new_item.model_dump_json()
    assert new_item.model_dump() == {
        **original_item.model_dump(),
        "status": "successful",
    }


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_update_by_tuple_key(redis_client, payload, freezer):
    """Calling update() with a tuple of keys updates the item if it exists already"""
    auth_logs = Collection(redis_client, schema=AuthLog, partial_schema=PartialAuthLog)
    payload = with_current_timestamps([payload], fields=["updated_at", "created_at"])[0]

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)
    key = (payload["job_id"], payload["app_token"])

    new_update = {
        "status": "successful",
        "job_id": payload["job_id"],
        "app_token": payload["app_token"],
    }
    new_item = auth_logs.update(key, PartialAuthLog(**new_update))
    new_item_in_db = _get_redis_value(redis_client, original_item)

    assert original_item_in_db == original_item.model_dump_json()
    assert new_item_in_db == new_item.model_dump_json()
    assert new_item.model_dump() == {
        **original_item.model_dump(),
        "status": "successful",
    }


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_dict_update_by_tuple_key(redis_client, payload, freezer):
    """Calling update() with a tuple of keys, and dict updates, changes the item if it exists already"""
    auth_logs = Collection(redis_client, schema=AuthLog)
    payload = with_current_timestamps([payload], fields=["updated_at", "created_at"])[0]

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)
    key = (payload["job_id"], payload["app_token"])

    new_update = {
        "status": "successful",
        "job_id": payload["job_id"],
        "app_token": payload["app_token"],
    }
    new_item = auth_logs.update(key, new_update)
    new_item_in_db = _get_redis_value(redis_client, original_item)

    assert original_item_in_db == original_item.model_dump_json()
    assert new_item_in_db == new_item.model_dump_json()
    assert new_item.model_dump() == {
        **original_item.model_dump(),
        "status": "successful",
    }


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_update_by_dict_key(redis_client, payload, freezer):
    """Calling update() with a primary key in dict form updates the item if it exists already"""
    auth_logs = Collection(redis_client, schema=AuthLog, partial_schema=PartialAuthLog)
    payload = with_current_timestamps([payload], fields=["updated_at", "created_at"])[0]

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)
    key = {
        "job_id": payload["job_id"],
        "app_token": payload["app_token"],
    }

    new_update = {"status": "successful", **key}
    new_item = auth_logs.update(key, PartialAuthLog(**new_update))
    new_item_in_db = _get_redis_value(redis_client, original_item)

    assert original_item_in_db == original_item.model_dump_json()
    assert new_item_in_db == new_item.model_dump_json()
    assert new_item.model_dump() == {
        **original_item.model_dump(),
        "status": "successful",
    }


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_dict_update_by_dict_key(redis_client, payload, freezer):
    """Calling update() with a primary key in dict form, and update dict, changes the item if it exists already"""
    auth_logs = Collection(redis_client, schema=AuthLog)
    payload = with_current_timestamps([payload], fields=["updated_at", "created_at"])[0]

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)
    key = {
        "job_id": payload["job_id"],
        "app_token": payload["app_token"],
    }

    new_update = {"status": "successful", **key}
    new_item = auth_logs.update(key, new_update)
    new_item_in_db = _get_redis_value(redis_client, original_item)

    assert original_item_in_db == original_item.model_dump_json()
    assert new_item_in_db == new_item.model_dump_json()
    assert new_item.model_dump() == {
        **original_item.model_dump(),
        "status": "successful",
    }


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_update_not_found(redis_client, payload, freezer):
    """Calling update() fails if the item does not exist"""
    auth_logs = Collection(redis_client, schema=AuthLog, partial_schema=PartialAuthLog)

    key_tuple = (payload["job_id"], payload["app_token"])
    single_key = "@@@".join(key_tuple)
    key_dict = {"app_token": payload["app_token"], "job_id": payload["job_id"]}

    new_update = {"status": "successful", **key_dict}

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.update(single_key, PartialAuthLog(**new_update))

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.update(single_key, new_update)

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.update(key_tuple, PartialAuthLog(**new_update))

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.update(key_tuple, new_update)

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.update(key_dict, PartialAuthLog(**new_update))

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.update(key_dict, new_update)

    hmap = _get_redis_hmap(redis_client, model=AuthLog)
    assert hmap == {}


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_update_invalid_model(redis_client, payload, freezer):
    """update() fails if the update payload does not satisfy the model"""
    auth_logs = Collection(redis_client, schema=AuthLog)

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)

    key_tuple = (payload["job_id"], payload["app_token"])
    single_key = _get_redis_key(original_item)
    key_dict = {"app_token": payload["app_token"], "job_id": payload["job_id"]}

    update = WrongAuthLog(**{**payload, "status": 9})
    with pytest.raises(ValidationError, match=r"validation error for PartialAuthLog"):
        auth_logs.update(single_key, update)

    with pytest.raises(ValidationError, match=r"validation error for PartialAuthLog"):
        auth_logs.update(single_key, update.model_dump())

    with pytest.raises(ValidationError, match=r"validation error for PartialAuthLog"):
        auth_logs.update(key_tuple, update)

    with pytest.raises(ValidationError, match=r"validation error for PartialAuthLog"):
        auth_logs.update(key_tuple, update.model_dump())

    with pytest.raises(ValidationError, match=r"validation error for PartialAuthLog"):
        auth_logs.update(key_dict, update)

    with pytest.raises(ValidationError, match=r"validation error for PartialAuthLog"):
        auth_logs.update(key_dict, update.model_dump())

    item_in_db = _get_redis_value(redis_client, original_item)
    assert item_in_db == original_item_in_db


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_update_indexes(redis_client, payload):
    """Calling update() updates the redis index keys"""
    auth_logs = Collection(redis_client, schema=AuthLog)

    first_item = AuthLog(**{"status": "pending", **payload})
    second_item = AuthLog(**{"status": "pending", **payload, "job_id": f"{uuid4()}"})

    auth_logs.insert(first_item)
    auth_logs.insert(second_item)
    first_redis_key = _get_redis_key(first_item)
    second_redis_key = _get_redis_key(second_item)
    first_key_bytes = first_redis_key.encode()
    second_key_bytes = second_redis_key.encode()

    new_update = {
        "status": "successful",
        "job_id": f'{payload["job_id"]}_{uuid4()}',
        "app_token": payload["app_token"],
    }
    new_first_item: AuthLog = auth_logs.update(first_redis_key, new_update)

    status_idx_prefix = _INDEX_PREFIXES["status"]
    job_id_idx_prefix = _INDEX_PREFIXES["job_id"]
    app_token_idx_prefix = _INDEX_PREFIXES["app_token"]

    # status changed
    new_first_status_idx_key = f"{status_idx_prefix}{new_first_item.status}"
    second_status_idx_key = f"{status_idx_prefix}{second_item.status}"

    assert redis_client.zrange(new_first_status_idx_key, 0, -1) == [first_key_bytes]
    assert redis_client.zrange(second_status_idx_key, 0, -1) == [second_key_bytes]

    # job_id changed
    new_first_job_id_idx_key = f"{job_id_idx_prefix}{new_first_item.job_id}"
    old_first_job_id_idx_key = f"{job_id_idx_prefix}{first_item.job_id}"
    second_job_id_idx_key = f"{job_id_idx_prefix}{second_item.job_id}"

    assert redis_client.zrange(new_first_job_id_idx_key, 0, -1) == [first_key_bytes]
    assert redis_client.zrange(second_job_id_idx_key, 0, -1) == [second_key_bytes]
    assert redis_client.zrange(old_first_job_id_idx_key, 0, -1) == []

    # app_token did not change, and was same in both
    app_token_idx_key = f"{app_token_idx_prefix}{new_first_item.app_token}"
    assert redis_client.zrange(app_token_idx_key, 0, -1) == sorted(
        [
            first_key_bytes,
            second_key_bytes,
        ]
    )


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_insert(redis_client, payload):
    """Calling insert() replaces the entire item with a new one, the optional TTL expiring"""
    auth_logs = Collection(redis_client, schema=AuthLog, cleanup_interval=0.1)

    basic_payload = {"status": "pending", **payload}
    persistent_item = AuthLog(**basic_payload)
    timestamped_item = AuthLog(**{**basic_payload, "job_id": f"per{payload["job_id"]}"})

    _insert_into_redis(redis_client, [persistent_item, timestamped_item])
    persistent_item_in_db = _get_redis_value(redis_client, persistent_item)
    timestamped_item_in_db = _get_redis_value(redis_client, timestamped_item)

    ttl = 0.3
    new_persistent_item = persistent_item.model_copy(
        update={"status": "successful", "created_at": "belle"}
    )
    new_timestamped_item = timestamped_item.model_copy(
        update={"status": "failed", "created_at": "beue"}
    )
    auth_logs.insert(new_persistent_item)
    auth_logs.insert(new_timestamped_item, ttl=ttl)

    time.sleep(ttl)

    new_persistent_item_from_api = auth_logs.get_one(
        (persistent_item.job_id, persistent_item.app_token)
    )
    with pytest.raises(ItemNotFoundError):
        auth_logs.get_one((timestamped_item.job_id, timestamped_item.app_token))

    # do a mutation like delete_many to force cleanup of raw db stuff
    auth_logs.delete_many(())

    new_persistent_item_in_db = _get_redis_value(redis_client, persistent_item)
    new_timestamped_item_in_db = _get_redis_value(redis_client, timestamped_item)

    assert persistent_item_in_db == persistent_item.model_dump_json()
    assert timestamped_item_in_db == timestamped_item.model_dump_json()

    assert new_persistent_item_from_api == new_persistent_item
    assert new_persistent_item_in_db == new_persistent_item.model_dump_json()
    assert new_timestamped_item_in_db is None


def test_insert_index(redis_client):
    """Calling insert() inserts some indexes"""
    auth_logs = Collection(redis_client, schema=AuthLog, cleanup_interval=0.1)
    item_ttl_pairs = [
        (AuthLog(**{"status": "pending", **payload}), None if i % 2 else 0.2)
        for i, payload in enumerate(_AUTH_LOG_LIST)
    ]

    expected_indexes = {}
    persistent_indexes = {}
    for item, ttl in item_ttl_pairs:
        auth_logs.insert(item, ttl=ttl)
        redis_item_key = _get_redis_key(item).encode()
        idx_keys = _get_redis_index_keys(item)
        for idx_key in idx_keys:
            original = expected_indexes.setdefault(idx_key, [])
            original.append(redis_item_key)
            if ttl is None:
                original = persistent_indexes.setdefault(idx_key, [])
                original.append(redis_item_key)

    for idx_key, expected in expected_indexes.items():
        got = redis_client.zrange(idx_key, 0, -1)
        assert sorted(got) == sorted(expected)

    time.sleep(0.2)
    # do a mutation call

    # do a mutation like delete_many to force cleanup of indexes
    auth_logs.delete_many(())

    for idx_key, expected in persistent_indexes.items():
        got = redis_client.zrange(idx_key, 0, -1)
        assert sorted(got) == sorted(expected)


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_insert_invalid_model(redis_client, payload, freezer):
    """insert() fails if the payload passed does not satisfy the model"""
    auth_logs = Collection(redis_client, schema=AuthLog)

    original_item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [original_item])
    original_item_in_db = _get_redis_value(redis_client, original_item)

    update = WrongAuthLog(**{**payload, "status": 9})
    with pytest.raises(ValidationError, match=r"validation error for AuthLog"):
        auth_logs.insert(update)

    item_in_db = _get_redis_value(redis_client, original_item)
    assert item_in_db == original_item_in_db


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_get_one(redis_client, payload, freezer):
    """Calling get_one() gets the item identified by the given key"""
    auth_logs = Collection(redis_client, schema=AuthLog)

    item = AuthLog(**{"status": "pending", **payload})
    _insert_into_redis(redis_client, [item])
    item_in_db = _get_redis_value(redis_client, item)

    single_key = _get_redis_key(item)
    key_tuple = (payload["job_id"], payload["app_token"])
    key_dict = {"app_token": payload["app_token"], "job_id": payload["job_id"]}

    item_by_single_key = auth_logs.get_one(single_key)
    item_by_key_tuple = auth_logs.get_one(key_tuple)
    item_by_key_dict = auth_logs.get_one(key_dict)

    assert item_in_db == item.model_dump_json()
    assert item_by_single_key == item
    assert item_by_key_tuple == item
    assert item_by_key_dict == item


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_get_one_not_found(redis_client, payload, freezer):
    """Calling get_one() raises ItemNotFoundError if item is nonexistent"""
    auth_logs = Collection(redis_client, schema=AuthLog)

    item = AuthLog(**{"status": "pending", **payload})

    single_key = _get_redis_key(item)
    key_tuple = (payload["job_id"], payload["app_token"])
    key_dict = {"app_token": payload["app_token"], "job_id": payload["job_id"]}

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.get_one(single_key)

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.get_one(key_tuple)

    with pytest.raises(ItemNotFoundError, match=r"not found"):
        auth_logs.get_one(key_dict)


@pytest.mark.parametrize("desc", [True, False])
def test_get_all(redis_client, desc, freezer):
    """Calling get_all() gets the items in the collection sorted by key"""
    items = [AuthLog(**{"status": "pending", **payload}) for payload in _AUTH_LOG_LIST]
    auth_logs = Collection(redis_client, schema=AuthLog)
    sorted_items = _sort_by_key(items, desc=desc)

    _insert_into_redis(redis_client, items)

    got = auth_logs.get_all(desc=desc)
    hmap = _get_redis_hmap(redis_client, model=AuthLog)
    expected_hmap = {_get_redis_key(v): v.model_dump_json() for v in sorted_items}

    assert got == sorted_items
    assert hmap == expected_hmap


def test_find_by_keys(redis_client, freezer):
    """Calling find_by_keys() gets the items with the given keys"""
    items = [AuthLog(**{"status": "pending", **payload}) for payload in _AUTH_LOG_LIST]
    auth_logs = Collection(redis_client, schema=AuthLog)

    for item in items:
        auth_logs.insert(item)

    single_keys = [_get_redis_key(v) for v in items]
    key_tuples = [(v.job_id, v.app_token) for v in items]
    key_dicts = [{"app_token": v.app_token, "job_id": v.job_id} for v in items]

    items_by_single_key = auth_logs.find_by_keys(*single_keys[1:], "non-existent")
    items_by_key_tuple = auth_logs.find_by_keys(*key_tuples)
    items_by_key_dict = auth_logs.find_by_keys(*key_dicts[:-1], "some-random")

    assert items_by_single_key == items[1:]
    assert items_by_key_tuple == items
    assert items_by_key_dict == items[:-1]


def test_find_by_index(redis_client, freezer):
    """Calling find_by_index() gets the items by searching in its index"""
    items = [AuthLog(**{"status": "pending", **payload}) for payload in _AUTH_LOG_LIST]
    _Partial = PartialAuthLog
    db = Collection(redis_client, schema=AuthLog, partial_schema=_Partial)

    for item in items:
        db.insert(item)

    def sort(data: List[AuthLog]) -> List[AuthLog]:
        """Sorts by job_id"""
        return sorted(data, key=lambda v: v.job_id)

    pending = JobStatus.PENDING
    executing = JobStatus.EXECUTING
    failed = JobStatus.EXECUTING

    pending_items = sort([v for v in items if v.status == pending])
    executing_items = sort([v for v in items if v.status == executing])
    failed_items = sort([v for v in items if v.status == failed])
    feet_items = sort([v for v in items if v.job_id == "feet"])
    bar_items = sort([v for v in items if v.app_token == "bar"])
    bar_pending_items = sort(
        [v for v in items if v.app_token == "bar" and v.status == pending]
    )
    bar_executing_items = sort(
        [v for v in items if v.app_token == "bar" and v.status == executing]
    )

    assert sort(db.find_by_index({"status": pending})) == pending_items
    assert sort(db.find_by_index({"status": executing})) == executing_items
    assert sort(db.find_by_index({"status": failed})) == failed_items
    assert sort(db.find_by_index({"job_id": "feet"})) == feet_items
    assert sort(db.find_by_index({"app_token": "bar"})) == bar_items
    assert (
        sort(db.find_by_index({"app_token": "bar", "status": pending}))
        == bar_pending_items
    )
    assert (
        sort(db.find_by_index({"app_token": "bar", "status": executing}))
        == bar_executing_items
    )

    assert sort(db.find_by_index(_Partial(status=pending))) == pending_items
    assert sort(db.find_by_index(_Partial(status=executing))) == executing_items
    assert sort(db.find_by_index(_Partial(status=failed))) == failed_items
    assert sort(db.find_by_index(_Partial(job_id="feet"))) == feet_items
    assert sort(db.find_by_index(_Partial(app_token="bar"))) == bar_items
    assert (
        sort(db.find_by_index(_Partial(app_token="bar", status=pending)))
        == bar_pending_items
    )
    assert (
        sort(db.find_by_index(_Partial(app_token="bar", status=executing)))
        == bar_executing_items
    )


@pytest.mark.parametrize("bounds", _DELETE_SLICES)
def test_delete_many_by_single_keys(redis_client, bounds: Tuple[int, int], freezer):
    """Calling delete_many() removes items with matching single key strings in the collection"""
    items = [AuthLog(**{"status": "pending", **payload}) for payload in _AUTH_LOG_LIST]
    auth_logs = Collection(redis_client, schema=AuthLog)
    keys_to_delete = [_get_redis_key(item) for item in items[bounds[0] : bounds[1]]]
    expected_items = items[: bounds[0]] + items[bounds[1] :]

    _insert_into_redis(redis_client, items)

    auth_logs.delete_many(keys_to_delete)
    hmap = _get_redis_hmap(redis_client, model=AuthLog)
    expected_hmap = {_get_redis_key(v): v.model_dump_json() for v in expected_items}

    assert hmap == expected_hmap


@pytest.mark.parametrize("bounds", _DELETE_SLICES)
def test_delete_many_by_key_tuples(redis_client, bounds: Tuple[int, int], freezer):
    """Calling delete_many() removes items with matching key tuples in the collection"""
    items = [AuthLog(**{"status": "pending", **payload}) for payload in _AUTH_LOG_LIST]
    auth_logs = Collection(redis_client, schema=AuthLog)
    keys_to_delete = [
        (item.job_id, item.app_token) for item in items[bounds[0] : bounds[1]]
    ]
    expected_items = items[: bounds[0]] + items[bounds[1] :]

    _insert_into_redis(redis_client, items)

    auth_logs.delete_many(keys_to_delete)
    hmap = _get_redis_hmap(redis_client, model=AuthLog)
    expected_hmap = {_get_redis_key(v): v.model_dump_json() for v in expected_items}

    assert hmap == expected_hmap


@pytest.mark.parametrize("bounds", _DELETE_SLICES)
def test_delete_many_by_key_dicts(redis_client, bounds: Tuple[int, int], freezer):
    """Calling delete_many() removes items with matching key dictionaries in the collection"""
    items = [AuthLog(**{"status": "pending", **payload}) for payload in _AUTH_LOG_LIST]
    auth_logs = Collection(redis_client, schema=AuthLog)
    keys_to_delete = [
        {"job_id": item.job_id, "app_token": item.app_token}
        for item in items[bounds[0] : bounds[1]]
    ]
    expected_items = items[: bounds[0]] + items[bounds[1] :]

    _insert_into_redis(redis_client, items)

    auth_logs.delete_many(keys_to_delete)
    hmap = _get_redis_hmap(redis_client, model=AuthLog)
    expected_hmap = {_get_redis_key(v): v.model_dump_json() for v in expected_items}

    assert hmap == expected_hmap


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_delete_indexes(redis_client, payload):
    """Calling delete() updates the redis index keys"""
    auth_logs = Collection(redis_client, schema=AuthLog)

    first_item = AuthLog(
        **{
            **payload,
            "status": "successful",
        }
    )
    second_item = AuthLog(**{**payload, "status": "pending", "job_id": f"{uuid4()}"})

    auth_logs.insert(first_item)
    auth_logs.insert(second_item)
    first_redis_key = _get_redis_key(first_item)
    second_redis_key = _get_redis_key(second_item)
    second_key_bytes = second_redis_key.encode()

    auth_logs.delete_many([first_redis_key])

    status_idx_prefix = _INDEX_PREFIXES["status"]
    job_id_idx_prefix = _INDEX_PREFIXES["job_id"]
    app_token_idx_prefix = _INDEX_PREFIXES["app_token"]

    # status changed
    first_status_idx_key = f"{status_idx_prefix}{first_item.status}"
    second_status_idx_key = f"{status_idx_prefix}{second_item.status}"

    assert redis_client.zrange(first_status_idx_key, 0, -1) == []
    assert redis_client.zrange(second_status_idx_key, 0, -1) == [second_key_bytes]

    # job_id changed
    first_job_id_idx_key = f"{job_id_idx_prefix}{first_item.job_id}"
    second_job_id_idx_key = f"{job_id_idx_prefix}{second_item.job_id}"

    assert redis_client.zrange(first_job_id_idx_key, 0, -1) == []
    assert redis_client.zrange(second_job_id_idx_key, 0, -1) == [second_key_bytes]

    # app_token was same in both
    app_token_idx_key = f"{app_token_idx_prefix}{first_item.app_token}"
    assert redis_client.zrange(app_token_idx_key, 0, -1) == [second_key_bytes]


def test_clear(redis_client, freezer):
    """Calling clear() removes all items in hashmap"""
    items = [AuthLog(**{"status": "pending", **payload}) for payload in _AUTH_LOG_LIST]
    auth_logs = Collection(redis_client, schema=AuthLog)

    _insert_into_redis(redis_client, items)

    auth_logs.clear()
    items_in_db = _get_redis_hmap(redis_client, model=AuthLog)

    assert items_in_db == {}


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_clear_indexes(redis_client, payload):
    """Calling clear() removes all the redis index keys"""
    auth_logs = Collection(redis_client, schema=AuthLog)

    first_item = AuthLog(
        **{
            **payload,
            "status": "successful",
        }
    )
    second_item = AuthLog(**{**payload, "status": "pending", "job_id": f"{uuid4()}"})

    auth_logs.insert(first_item)
    auth_logs.insert(second_item)

    auth_logs.clear()

    status_idx_prefix = _INDEX_PREFIXES["status"]
    job_id_idx_prefix = _INDEX_PREFIXES["job_id"]
    app_token_idx_prefix = _INDEX_PREFIXES["app_token"]

    # status changed
    first_status_idx_key = f"{status_idx_prefix}{first_item.status}"
    second_status_idx_key = f"{status_idx_prefix}{second_item.status}"

    assert redis_client.zrange(first_status_idx_key, 0, -1) == []
    assert redis_client.zrange(second_status_idx_key, 0, -1) == []

    # job_id changed
    first_job_id_idx_key = f"{job_id_idx_prefix}{first_item.job_id}"
    second_job_id_idx_key = f"{job_id_idx_prefix}{second_item.job_id}"

    assert redis_client.zrange(first_job_id_idx_key, 0, -1) == []
    assert redis_client.zrange(second_job_id_idx_key, 0, -1) == []

    # app_token was same in both
    app_token_idx_key = f"{app_token_idx_prefix}{first_item.app_token}"
    assert redis_client.zrange(app_token_idx_key, 0, -1) == []


@pytest.mark.parametrize("payload", _AUTH_LOG_LIST)
def test_exists(redis_client, payload, freezer):
    """Calling exists() checks that key exists"""
    auth_logs = Collection(redis_client, schema=AuthLog)

    item = AuthLog(**{"status": "pending", **payload})

    single_key = _get_redis_key(item)
    key_tuple = (payload["job_id"], payload["app_token"])
    key_dict = {"app_token": payload["app_token"], "job_id": payload["job_id"]}

    assert not auth_logs.exists(single_key)
    assert not auth_logs.exists(key_tuple)
    assert not auth_logs.exists(key_dict)

    _insert_into_redis(redis_client, [item])

    assert auth_logs.exists(single_key)
    assert auth_logs.exists(key_tuple)
    assert auth_logs.exists(key_dict)


def _get_current_timestamp():
    """Gets the current timestamp"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_redis_value(redis: Redis, item: Schema) -> Union[str, None]:
    """Get the value of the item in redis

    Args:
        redis: the redis connection
        item: the item that might have been stored in the redis

    Returns:
        the value saved in redis for the given item or None if not exists
    """
    hashmap_name = _get_redis_hashmap_name(item.__class__)
    redis_key = _get_redis_key(item)
    value: Union[bytes, None] = redis.hget(hashmap_name, redis_key)
    if isinstance(value, bytes):
        return value.decode()

    return value


def _get_redis_indexes(redis: Redis, item: Schema) -> Union[str, None]:
    """Get the value of the indexes for the item in redis

    Args:
        redis: the redis connection
        item: the item that might have been stored in the redis

    Returns:
        the indexes of value saved in redis for the given item or None if they don't exist
    """
    hashmap_name = _get_redis_hashmap_name(item.__class__)
    redis_key = _get_redis_key(item)
    value: Union[bytes, None] = redis.hget(hashmap_name, redis_key)
    if isinstance(value, bytes):
        return value.decode()

    return value


def _get_redis_hmap(redis: Redis, model: Type[Schema]) -> Dict[str, str]:
    """Get the full hashmap for the given model in redis sorted by key

    Args:
        redis: the redis connection
        model: the model whose collection is to be queried

    Returns:
        the dictionary of keys and values in the redis hashmap of the model
    """
    hashmap_name = _get_redis_hashmap_name(model)
    raw_value: Dict[bytes, bytes] = redis.hgetall(hashmap_name)
    return {k.decode(): v.decode() for k, v in raw_value.items()}


def _insert_into_redis(redis: Redis, items: List[Schema]):
    """Inserts the items into redis

    Args:
        redis: the redis connection
        items: the items that are to be inserted
    """
    try:
        hashmap_name = _get_redis_hashmap_name(items[0].__class__)
    except IndexError:
        return

    redis_items = {_get_redis_key(item): item.model_dump_json() for item in items}
    redis.hset(hashmap_name, mapping=redis_items)


def _get_redis_hashmap_name(model: Type[Schema]) -> Union[str, None]:
    """Get the name of the hashmap for the given model

    Args:
        model: the model under consideration

    Returns:
        the name of the hashmap where the item is stored
    """
    return f"{model.__module__}.{model.__qualname__}".lower()


def _get_redis_key(item: Schema) -> Union[str, None]:
    """Get the key of the item in redis

    Args:
        item: the item under consideration

    Returns:
        the key for the given item
    """
    keys = tuple(
        getattr(item, field) for field in item.__class__.__primary_key_fields__
    )
    return "@@@".join(keys)


def _get_redis_index_keys(item: Schema) -> List[str]:
    """Get the keys of the indexes of the item in redis

    Args:
        item: the item under consideration

    Returns:
        the keys of the indexes for the given item
    """
    return [
        f"{prefix}{getattr(item, prop)}" for prop, prefix in _INDEX_PREFIXES.items()
    ]


def _sort_by_key(items: List[Schema], desc: bool = False) -> List[Schema]:
    """Sorts the items by the key

    Args:
        items: the items to sort
        desc: whether to return in ascending or descending order

    Returns:
        the items sorted
    """
    return sorted(items, key=lambda v: _get_redis_key(v), reverse=desc)


class WrongAuthLog(Schema):
    __primary_key_fields__ = ("job_id", "app_token")

    job_id: str
    app_token: str
    status: int
