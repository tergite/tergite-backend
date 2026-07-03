# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2025, 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Utilities for handling authentication of the connection to MSS"""
import base64
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

_BCC_PRIVATE_KEYS: Dict[str, RSAPrivateKey] = {}


def create_headers(
    private_key_file: Path,
    device: str = "",
    key_password: Optional[bytes] = None,
) -> dict[str, str]:
    """Creates headers to show that the request is a valid one from BCC

    Args:
        private_key_file: the path to the private key file
        device: the name of this device
        key_password: the password used to encrypt the key PEM file

    Returns:
        The dict of headers that show a given request is from BCC
    """
    request_id = str(uuid.uuid4())
    timestamp = time.time()
    message = f"{device}-{request_id}-{timestamp}"
    signature = sign_message(private_key_file, message=message, password=key_password)
    return {
        "x-request-id": request_id,
        "x-timestamp": f"{timestamp}",
        "x-signature": signature,
        "x-id": device,
    }


def sign_message(key_file: Path, message: str, password: Optional[bytes]) -> str:
    """Creates an BCC-signed signature given a message

    Args:
        key_file: the path to the private RSA key
        message: the message
        password: the password used to encrypt the key PEM file

    Returns:
        the string form of the signature
    """
    mss_private_key = get_private_key(key_file, password=password)
    signature = mss_private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def get_private_key(key_file: Path, password: Optional[bytes]) -> RSAPrivateKey:
    """Loads the private key for BCC

    Args:
        key_file: the path to the private key file
        password: the password that the private key was encrypted with

    Returns:
        the private key of the BCC
    """
    global _BCC_PRIVATE_KEYS

    key_file_str = str(key_file)

    try:
        return _BCC_PRIVATE_KEYS[key_file_str]
    except KeyError:
        with open(key_file, "rb") as file:
            key = _BCC_PRIVATE_KEYS[key_file_str] = serialization.load_pem_private_key(
                file.read(), password=password
            )
        return key
