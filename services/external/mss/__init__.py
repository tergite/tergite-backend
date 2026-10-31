# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""The Main Service Server (MSS): the client to it, its events and the signatures it trusts"""
from .auth import (
    encrypt_mss_jwt_token,
    get_mss_public_key,
    get_private_key,
    verify_mss_signature,
)
from .dtos import DeviceEvent, DeviceEventName, DeviceSwitchData, EventResponse
from .events import send_kept_mss_events, send_mss_event
from .nonces import claim_mss_nonce, delete_expired_mss_nonces
from .service import MssClient

__all__ = [
    "DeviceEvent",
    "DeviceEventName",
    "DeviceSwitchData",
    "EventResponse",
    "MssClient",
    "claim_mss_nonce",
    "delete_expired_mss_nonces",
    "encrypt_mss_jwt_token",
    "get_mss_public_key",
    "get_private_key",
    "send_kept_mss_events",
    "send_mss_event",
    "verify_mss_signature",
]
