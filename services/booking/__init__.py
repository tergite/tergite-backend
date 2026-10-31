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
"""Bookings of the device and its users, as MSS knows them"""
from .dtos import (
    Booking,
    BookingsConfig,
    MSSTokenClaims,
    NewBookingInfo,
    NewUserInfo,
    User,
    UserProfile,
)
from .service import (
    create_booking,
    create_mss_jwt_token,
    create_user,
    delete_bookings,
    delete_users,
    get_active_booking,
    get_booking,
    get_many_bookings,
    get_many_user_profiles,
    get_next_booking,
    get_or_create_user,
    get_user,
    get_user_job_id_pair_from_token,
    get_user_profile,
)

__all__ = [
    "Booking",
    "BookingsConfig",
    "MSSTokenClaims",
    "NewBookingInfo",
    "NewUserInfo",
    "User",
    "UserProfile",
    "create_booking",
    "create_mss_jwt_token",
    "create_user",
    "delete_bookings",
    "delete_users",
    "get_active_booking",
    "get_booking",
    "get_many_bookings",
    "get_many_user_profiles",
    "get_next_booking",
    "get_or_create_user",
    "get_user",
    "get_user_job_id_pair_from_token",
    "get_user_profile",
]
