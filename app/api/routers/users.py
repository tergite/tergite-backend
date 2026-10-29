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
"""Routes for the users, their profiles and their job tokens"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Engine

from app.services import booking, scheduler
from app.services.booking import MSSTokenClaims, NewUserInfo, UserProfile
from app.utils.exc import UnauthorizedError

from ...services.external.mss import encrypt_mss_jwt_token
from ...utils.messages import GeneralMessage
from ..dependencies import (
    DeviceResourcesDep,
    check_device_is_online,
    get_booking_db,
    get_verified_mss_admin_user_id,
    get_verified_mss_user_id,
)
from ..dtos import PaginatedListResponse, TokenResponse

router = APIRouter()


@router.get("/me")
async def view_profile(
    db_engine: Engine = Depends(get_booking_db),
    user_id: str = Depends(get_verified_mss_user_id),
) -> UserProfile:
    """Views the profile of the current user

    Args:
        db_engine: the SQL database to query
        user_id: the user_id as submitted by MSS

    Raises:
        ItemNotFoundError (404): user not found

    Returns:
        the user profile of the current user
    """
    return booking.get_user_profile(db_engine, user_id)


@router.delete("/me", dependencies=[Depends(check_device_is_online)])
async def delete_profile(
    device: DeviceResourcesDep,
    user_id: str = Depends(get_verified_mss_user_id),
) -> GeneralMessage:
    """Deletes the profile of the current user

    Args:
        device: what the device runs on
        user_id: the user_id as submitted by MSS

    Raises:
        ItemNotFoundError (404): user not found

    Returns:
        A general message object with status
    """
    scheduler.delete_user_profile(device, user_id=user_id)
    return GeneralMessage(status="success", detail="Profile deleted")


@router.post("/token")
async def get_mss_token(
    device: DeviceResourcesDep,
    body: MSSTokenClaims,
    mss_user_id: str = Depends(get_verified_mss_user_id),
) -> TokenResponse:
    """Get a token specific to the associated MSS instance for the provided token claims.

    This is a special route as it can only be used through the associated MSS instance.
    On top of that MSS being whitelisted, the public key of that MSS will be used to encrypt
    the access token that will be sent over to the user.

    Take note that the JWT must also contain the `user_id` as well as the `job_id`
    as these will be used to ensure a JWT is not used for another job.

    Take note also that if the given user does not exist, the user is created with
    a random email.

    Args:
        device: what the device runs on
        body: the http request body sent
        mss_user_id: the user_id as specified by MSS

    Returns:
        the user token response
    """
    user_id = body.user_id
    job_id = body.job_id

    if user_id != mss_user_id:
        # just some house-keeping to ensure the two user ids are the same
        raise UnauthorizedError("Forbidden")

    user = booking.get_or_create_user(device.bookings_db, user_id)

    # create encrypted token
    token = booking.create_mss_jwt_token(
        user,
        job_id=job_id,
        secret_key=device.settings.jwt_secret,
        ttl=device.settings.jwt_ttl,
    )
    encrypted_token = encrypt_mss_jwt_token(token, device.mss_public_key)
    return {"access_token": encrypted_token, "token_type": "bearer"}


@router.post("/users", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def create_user(
    data: NewUserInfo, db_engine: Engine = Depends(get_booking_db)
) -> UserProfile:
    """Creates a user given the name and email

    Only MSS admin users can create users here

    Args:
        data: the information about the new user
        db_engine: the SQL database to query

    Returns:
        the created user
    """
    user = booking.create_user(db_engine, data=data)
    return UserProfile.from_user(user)


@router.delete(
    "/users/{user_id}",
    dependencies=[
        Depends(get_verified_mss_admin_user_id),
        Depends(check_device_is_online),
    ],
)
async def remove_user(
    device: DeviceResourcesDep,
    user_id: str,
) -> GeneralMessage:
    """Deletes the user of the given user_id

    Only admins are allowed to remove users

    Args:
        device: what the device runs on
        user_id: the unique identifier of the user

    Raises:
        ItemNotFoundError: user not found

    Returns:
        A general message object with status
    """
    scheduler.delete_user_profile(device, user_id=user_id)
    return GeneralMessage(status="success", detail="User deleted")


@router.get("/users", dependencies=[Depends(get_verified_mss_admin_user_id)])
async def view_users(
    skip: int = Query(default=0),
    limit: Optional[int] = Query(default=None),
    db_engine: Engine = Depends(get_booking_db),
) -> PaginatedListResponse[UserProfile]:
    """Views all users

    Only MSS admin users can view this

    Args:
        skip: number of records to ignore at the top of the returned results; default is 0
        limit: maximum number of records to return; default is None.
        db_engine: the SQL database to query

    Returns:
        the paginated list of the available users
    """
    data = booking.get_many_user_profiles(db_engine, skip=skip, limit=limit)
    return PaginatedListResponse(skip=skip, limit=limit, data=data)
