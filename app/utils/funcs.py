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
"""General utility functions, some of which can be dynamically imported to process a job"""

import logging
import random
import time
from importlib import import_module
from typing import TYPE_CHECKING, Callable, Tuple

if TYPE_CHECKING:
    from .queues.dtos import Job, QueueContext


def preprocess(job: "Job", context: "QueueContext") -> Tuple[float, "QueueContext"]:
    """A dummy preprocessing function that just generates a random duration if job has none

    Args:
        job: the job to preprocess
        context: the context in the queue

    Returns:
        the pair of the duration estimate and the queue context
    """
    logging.info(f"starting preprocessing of job {job.job_id}")
    duration = job.estimated_duration
    if duration is None:
        # FIXME: Just some dummy functionality to compute dummy duration of the job
        duration = random.uniform(0, 2)
    logging.info(f"completed preprocessing of job {job.job_id}")
    return duration, context


def execute(job: "Job", context: "QueueContext") -> Tuple[str, "QueueContext"]:
    """A dummy execution function that just sleeps for the given estimate duration

    Args:
        job: the job to preprocess
        context: the context in the queue

    Returns:
        the pair of the job ID and the queue context
    """
    logging.info(f"starting execution of job {job.job_id}")
    duration = 0
    if job.estimated_duration:
        duration = job.estimated_duration
    time.sleep(duration)
    logging.info(f"completed execution of job {job.job_id}")
    return job.job_id, context


def noop(*args, **kwargs):
    """No operation"""
    pass


def import_func(func_path: str) -> Callable:
    """Imports a function that has been identified by its import path

    Args:
        func_path: the path to the function

    Returns:
        the imported function

    Raises:
        ImportError: failed to import {func_path}
    """
    try:
        module_name, func_name = func_path.rsplit(".", maxsplit=1)
        module = import_module(module_name)
        return getattr(module, func_name)
    except (ImportError, AttributeError, ValueError) as exp:
        logging.error(exp)
        raise ImportError(f"failed to import {func_path}")


def get_function_import_path(func: Callable) -> str:
    """Gets the full import path of the given function

    Args:
        func: the function whose import path is needed

    Returns:
        the import path of the function
    """
    return f"{func.__module__}.{func.__qualname__}"
