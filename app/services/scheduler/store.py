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
"""Module containing the store for the scheduler service"""


from pydantic import RedisDsn
from redis import Redis

import settings

from ...libs.queues.dtos import Job
from ...utils.redis import get_redis_connection
from ...utils.redis_store import Collection

_JOB_STORES: dict[str, Collection[Job]] = {}


def get_jobs_store(
    url: RedisDsn | str | None = None,
    default_ttl: float | None = None,
    cleanup_interval: float | None = None,
) -> Collection[Job]:
    """Gets the store for the given url for the jobs

    Args:
        url: the database URL for the redis server; default = settings.RQ_REDIS_URL
        default_ttl: the default TTL for the items saved; default = settings.JOBS_STORE_TTL
        cleanup_interval: the cleanup interval for indexes of the jobs store;
            default = settings.JOBS_STORE_CLEAN_INTERVAL

    Returns:
        the RedisCollection containing the jobs
    """
    global _JOB_STORES
    url = f"{url or settings.RQ_REDIS_URL}"
    job_store = _JOB_STORES.get(url)
    if job_store is None:
        connection = get_redis_connection(url)
        job_store = _init_jobs_store(
            connection=connection,
            default_ttl=settings.JOBS_STORE_TTL if default_ttl is None else default_ttl,
            cleanup_interval=(
                settings.JOBS_STORE_CLEAN_INTERVAL
                if cleanup_interval is None
                else cleanup_interval
            ),
        )
        _JOB_STORES[url] = job_store
    return job_store


def clear_jobs_stores_registry() -> None:
    """Clears the jobs stores registry"""
    global _JOB_STORES
    _JOB_STORES.clear()


def _init_jobs_store(
    connection: Redis, default_ttl: float, cleanup_interval: float
) -> Collection[Job]:
    """Initializes the store for the given redis connection for the jobs

    Args:
        connection: the connection to the redis server
        default_ttl: the default TTL for the items saved
        cleanup_interval: the cleanup interval for indexes of the jobs store

    Returns:
        the RedisCollection containing the jobs
    """
    return Collection(
        connection=connection,
        schema=Job,
        default_ttl=default_ttl,
        cleanup_interval=cleanup_interval,
    )
