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
from pydantic import RedisDsn

import settings
from app.utils.logging import err_logger

_REDIS_CONNECTIONS: dict[str, redis.Redis] = {}


def get_redis_connection(url: RedisDsn | str = settings.RQ_REDIS_URL) -> redis.Redis:
    """Gets the redis connection

    Args:
        url: the URL to the redis server

    Returns:
        the redis connection
    """
    global _REDIS_CONNECTIONS
    url = f"{url}"

    connection = _REDIS_CONNECTIONS.get(url)

    if connection is None:
        kwargs = {}
        if url.startswith("rediss:"):
            kwargs["ssl"] = True

        connection = redis.Redis.from_url(url, **kwargs)
        _REDIS_CONNECTIONS[url] = connection
    return connection


def clear_redis_connections(ignore_errors: bool = False) -> None:
    """Clears the redis connections

    Args:
        ignore_errors: If True, ignore any errors raised by redis connections
    """
    global _REDIS_CONNECTIONS
    for connection in _REDIS_CONNECTIONS.values():
        try:
            connection.close()
        except Exception as exp:
            if ignore_errors:
                err_logger.warning(f"Close redis connection error: {exp}")
            else:
                raise exp

    _REDIS_CONNECTIONS.clear()
