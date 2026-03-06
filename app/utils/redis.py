# This code is part of Tergite
#
# (C) Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Module containing the utils for redis"""
import redis
import redis.asyncio as async_redis
from pydantic import RedisDsn


def get_redis_connection(
    url: RedisDsn | str, is_async: bool = False
) -> redis.Redis | async_redis.Redis:
    """Gets the redis connection

    Args:
        url: the URL to the redis server
        is_async: whether to use async redis connection

    Returns:
        the redis connection
    """
    cls = async_redis.Redis if is_async else redis.Redis

    redis_url = f"{url}"
    if redis_url.startswith("rediss:"):
        return cls.from_url(redis_url, ssl=True)
    return cls.from_url(redis_url)
