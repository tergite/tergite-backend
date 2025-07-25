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
from importlib import import_module
from typing import Callable


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
