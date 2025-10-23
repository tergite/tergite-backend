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
"""Module containing utilities for the booking service"""
import bcrypt


def hash_password(password: str) -> str:
    """Converts the password into a hash to obfuscate it

    Args:
        password: the password to obfuscate

    Returns:
        the password hash
    """
    pwd_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(password=pwd_bytes, salt=salt)
    return hashed_password.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verifies that the password corresponds to the password hash

    Args:
        password: the plain password being checked
        password_hash: the obfuscated password to test against

    Returns:
        True if the password matches with password hash else False
    """
    password_bytes = password.encode("utf-8")
    password_hash_bytes = password_hash.encode("utf-8")
    return bcrypt.checkpw(password=password_bytes, hashed_password=password_hash_bytes)
