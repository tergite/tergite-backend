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
"""Utilities specific to rq"""

from redis import Redis
from rq import exceptions as rq_errors
from rq.job import Job as RqJob


def cancel_rq_job(connection: Redis, job_id: str, ignore_errors: bool = True):
    """Cancels the job if it exists

    Args:
        connection: the connection to the redis server where the queues are
        job_id: the id of the rq job
        ignore_errors: whether to ignore errors; default = True

    Raises:
        rq.exceptions.NoSuchJobError: if the job does not exist
        rq.exceptions.InvalidJobOperationError: if the job has already been cancelled
        rq.exceptions.InvalidJobOperation: if the job has already been cancelled
    """
    try:
        job = RqJob.fetch(job_id, connection=connection)
        job.cancel()
    except (
        rq_errors.NoSuchJobError,
        rq_errors.InvalidJobOperationError,
        rq_errors.InvalidJobOperation,
    ) as exp:
        if not ignore_errors:
            raise exp
