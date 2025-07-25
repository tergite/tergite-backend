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

from redis import Redis

from ...utils.queues.dtos import Job
from ...utils.redis_store import Collection


def get_jobs_store(url: str) -> Collection:
    """Gets the store for the given url for the jobs

    Args:
        url: the database URL for the redis server

    Returns:
        the RedisCollection containing the jobs
    """
    connection = Redis.from_url(url=url)
    return Collection(connection=connection, schema=Job)
