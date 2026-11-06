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

from typing import Optional


class BaseBccException(Exception):
    """The base of BCC's errors, whose message is its string"""

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


class InvalidBookingError(BaseBccException):
    """Error when a booking starts in the past or lasts outside the device's limits"""


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


class PostProcessingError(BaseBccException):
    """Exception raised when something unexpected happens during postprocessing"""

    def __init__(self, exp: Exception, job_id: str):
        super().__init__(f"{exp}")
        self.exp = exp
        self.job_id = job_id

    def __repr__(self):
        return f"{self.__class__.__name__}<job_id: {self.job_id}, {repr(self.exp)}>"


class JobAlreadyCancelled(BaseBccException):
    """Exception when Job is already cancelled"""


class InvalidJobIdInUploadedFileError(BaseBccException):
    """Exception when an uploaded job file has no valid job id"""

    pass


class InvalidRequestError(BaseBccException):
    """Exception when an invalid HTTP request is made"""


class IsOfflineError(BaseBccException):
    """Exception when a device is offline"""


class MssRejectedError(BaseBccException):
    """Error when MSS answers a request of ours with an error"""


class MssTimeoutError(BaseBccException):
    """Error when MSS does not answer a request of ours in time"""


class UnsupportedCircuitError(BaseBccException):
    """Error when a gate-level job's circuit cannot be compiled for the device

    Attributes:
        line: the line of the circuit's syntax error, from 1, if it is one whose place
            is known
        column: the column of that syntax error, from 0
    """

    def __init__(
        self,
        message: str = "",
        line: Optional[int] = None,
        column: Optional[int] = None,
    ):
        super().__init__(message)
        self.line = line
        self.column = column


class UncomputableExpressionError(UnsupportedCircuitError):
    """Error when an expression of a circuit cannot be computed, as on a division by zero

    Attributes:
        reason: why it cannot, e.g. division by zero
    """

    def __init__(self, reason: str):
        super().__init__(f"an expression cannot be computed: {reason}")
        self.reason = reason


class InvalidCircuitError(BaseBccException):
    """Error when a job file holds a circuit the device cannot run, whatever its state

    Attributes:
        circuit_index: the index of the circuit in the job file's circuits
        line: the line of the circuit's syntax error, from 1, if it is one whose place
            is known
        column: the column of that syntax error, from 0
    """

    def __init__(
        self,
        message: str,
        circuit_index: int,
        line: Optional[int] = None,
        column: Optional[int] = None,
    ):
        super().__init__(message)
        self.circuit_index = circuit_index
        self.line = line
        self.column = column
