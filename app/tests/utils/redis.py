# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2024
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Fixtures of the Redis the tests run on"""
from typing import Any, Generator

import pytest
import redis
from redis.client import Redis

from app.tests.utils.env import TEST_RQ_REDIS_URL

_redis_connection = redis.Redis.from_url(TEST_RQ_REDIS_URL)


@pytest.fixture
def redis_client() -> Generator[Redis, Any, None]:
    """The test Redis database, emptied after the test"""
    yield _redis_connection
    _redis_connection.flushdb()
