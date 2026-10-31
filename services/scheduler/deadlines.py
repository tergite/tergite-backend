# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""The deadline a worker sets itself, which the process that started it checks from outside

A worker stuck in a call that never returns to Python cannot notice it, as neither its timers'
signal handlers nor a thread of its own would tell; the process that started it can.
"""
import math
import multiprocessing
import time


class WorkerDeadline:
    """The time by which a worker is to set its deadline again, on the monotonic clock, which
    every process of the machine shares; shared memory, so that it crosses processes as an
    argument of the worker's process"""

    def __init__(self):
        # none, until the worker sets one
        self._monotonic_time = multiprocessing.Value("d", math.inf, lock=False)

    def set_in(self, seconds: float) -> None:
        """Sets the deadline the seconds from now

        Args:
            seconds: the seconds from now; math.inf for no deadline
        """
        self._monotonic_time.value = time.monotonic() + seconds

    def get_seconds_left(self) -> float:
        """Gets the seconds to the deadline

        Returns:
            the seconds left, negative once it has passed, math.inf while there is none
        """
        return self._monotonic_time.value - time.monotonic()
