# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright Martin Ahindura 2023
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
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey


def create_headers(private_key: RSAPrivateKey, device: str = "") -> dict[str, str]:
    """Creates headers to show that the request is a valid one from BCC

    Args:
        private_key: the private key of this BCC
        device: the name of this device

    Returns:
        The dict of headers that show a given request is from BCC
    """
    request_id = str(uuid.uuid4())
    timestamp = time.time()
    message = f"{device}-{request_id}-{timestamp}"
    signature = sign_message(private_key, message=message)
    return {
        "x-request-id": request_id,
        "x-timestamp": f"{timestamp}",
        "x-signature": signature,
        "x-id": device,
    }


def sign_message(private_key: RSAPrivateKey, message: str) -> str:
    """Creates an BCC-signed signature given a message

    Args:
        private_key: the private key of this BCC
        message: the message

    Returns:
        the string form of the signature
    """
    signature = private_key.sign(
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
    with open(key_file, "rb") as file:
        return serialization.load_pem_private_key(file.read(), password=password)


def encrypt_mss_jwt_token(token: str, mss_public_key: RSAPublicKey) -> str:
    """Encrypts the token passed so that it is only readable by the right MSS instance

    Args:
        token: the raw token to encrypt
        mss_public_key: the public key of that MSS

    Returns:
        the encrypted token
    """
    cipher_bytes = mss_public_key.encrypt(
        token.encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(cipher_bytes).decode()


def verify_mss_signature(
    signature: str, message: str, mss_public_key: RSAPublicKey
) -> None:
    """Verifies that the given message is from MSS, given the signature

    Args:
        signature: the signature of the message signed by MSS
        message: the message from MSS
        mss_public_key: the public key of that MSS

    Raises:
        InvalidSignature: if signature does not match with what would be expected from MSS
    """
    mss_public_key.verify(
        base64.b64decode(signature),
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256(),
    )


def get_mss_public_key(key_path: Path) -> RSAPublicKey:
    """Loads the public key for MSS given the path to the key file

    Args:
        key_path: the path to the PEM file of the key

    Returns:
        the MSS public key
    """
    with open(key_path, "rb") as key_file:
        return serialization.load_pem_public_key(key_file.read())
