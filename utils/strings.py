# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2021
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
# Modified:
#
# - Martin Ahindura 2023
#
"""Utilities for strings"""
import random
import string
import uuid


def validate_uuid4_str(id_str):
    return validate_uuid_str(id_str, 4)


def validate_uuid_str(id_str, version):
    try:
        temp_uuid = uuid.UUID(id_str, version=version)
    except (ValueError, TypeError):
        return False
    return str(temp_uuid) == id_str


def uuid_str() -> str:
    """generates a UUID string"""
    return f"{uuid.uuid4()}"


def get_random_name(length: int = 10) -> str:
    """Generates a pseudo random name all in lower case

    Args:
        length: the length of the name

    Returns:
        a random name in lower case
    """
    char_set = string.ascii_lowercase
    return "".join(random.sample(char_set * length, length))
