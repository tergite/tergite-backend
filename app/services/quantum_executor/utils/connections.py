# This code is part of Tergite
#
# (C) Martin Ahindura (2024)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Utilities for connections to quantum_executor service"""
from filelock import FileLock


def get_executor_lock():
    """Get a lock on the quantum executor to avoid interference when controlling it"""
    return FileLock(".quantum-executor.lock")
