# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright Martin Ahindura 2023
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""The shapes of the API's request bodies and responses"""

from typing import Generic, List, Literal, Optional, TypedDict, TypeVar

from pydantic import BaseModel

ITEM = TypeVar("ITEM", bound=BaseModel)


class InvalidCircuitDetail(BaseModel):
    """Why a job file's circuit was refused at upload

    line, from 1, and column, from 0, are those of a syntax error outside the circuit's
    cal blocks, and None for any other error.
    """

    message: str
    circuit_index: int
    line: Optional[int]
    column: Optional[int]


class InvalidCircuitResponse(BaseModel):
    """The body of the 422 that refuses a job file's circuit at upload"""

    detail: InvalidCircuitDetail


class CancellationDetails(BaseModel):
    """Details to do with a given cancellation request"""

    reason: Optional[str] = None


class TokenResponse(TypedDict):
    """A token response sent on the API"""

    access_token: str
    token_type: Literal["bearer",]


class PaginatedListResponse(BaseModel, Generic[ITEM]):
    """The response when sending paginated data"""

    skip: int = 0
    limit: Optional[int] = None
    data: List[ITEM] = []
