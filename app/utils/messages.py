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
"""The message the API and the services answer with when there is no item to return"""
from typing import Literal, NotRequired, TypedDict


class GeneralMessage(TypedDict):
    """A general message object sent on the API"""

    status: Literal["success", "error", "cancelled", "failed"]
    detail: NotRequired[str]
    data: NotRequired[dict | list]
