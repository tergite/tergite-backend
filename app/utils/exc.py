# This code is part of Tergite
#
# (C) Nicklas Botö, Fabian Forslund 2022
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
"""Utilities for exceptions"""


class BaseBccException(Exception):
    def __init__(self, message: str = ""):
        self._message = message

    def __repr__(self):
        return f"{self.__class__.__name__}: {self._message}"

    def __str__(self):
        return self._message if self._message else self.__class__.__name__


class JobAlreadyCompleteError(BaseBccException):
    """Error when the complete job is treated as an incomplete one"""


class ItemNotFoundError(BaseBccException):
    """Exception when item is not found"""


class ConflictError(BaseBccException):
    """Error when an item conflicts with another"""


class MaxBookingsError(BaseBccException):
    """Error when there are too many bookings in a given period"""


class BookingAlreadyCompleteError(BaseBccException):
    """Error when a booking is already complete, yet it is treated as if it is not"""


class BookingAlreadyActiveError(BaseBccException):
    """Error when a booking is already active, yet it is treated as if it has not"""


class NotAuthenticatedError(BaseBccException):
    """Error when a user is not authenticated"""


class UnauthorizedError(BaseBccException):
    """Error when a user is not authorized to do something"""


class NotAllowedError(BaseBccException):
    """Error when a given operation is not allowed"""


class NotInstalledError(BaseBccException):
    """when a given dependency is not installed"""


class PostProcessingError(Exception):
    """Exception raised when something unexpected happens during postprocessing"""

    def __init__(self, exp: Exception, job_id: str):
        self.exp = exp
        self.job_id = job_id

    def __repr__(self):
        return f"{self.__class__.__name__}<job_id: {self.job_id}, {repr(self.exp)}>"


class JobAlreadyCancelled(BaseBccException):
    """Exception when Job is already cancelled"""


class InvalidJobIdInUploadedFileError(BaseBccException):
    pass
