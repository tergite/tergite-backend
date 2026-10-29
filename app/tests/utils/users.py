# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Utilities for the users the tests create through the API"""
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)
from uuid import uuid4

from fastapi.testclient import TestClient
from httpx import Response

from app.tests.utils.api import (
    create_mss_headers,
)
from app.tests.utils.params import USERS


def create_user(client: "TestClient", user: dict) -> Tuple[str, "Response"]:
    """Creates a user and returns their ID and the httpx.Response

    Args:
        client: the fastapi TestClient used for testing
        user: the user info for creating the user

    Returns:
        the tuple of id of the user and the result
    """
    headers = create_mss_headers(is_admin=True)
    user_id = f"{uuid4()}"
    payload = {**user, "id": user_id}

    response = client.post("/users", json=payload, headers=headers)
    json_response = response.json()

    assert json_response["id"] == user_id
    return user_id, response


def create_many_users(
    client: "TestClient",
    raw_users: List[Dict[str, Any]] = tuple(USERS),
) -> List[Dict[str, Any]]:
    """Creates a list of users, returning them as dicts

    Args:
        client: the fastapi test client
        raw_users: the list of raw user data

    Returns:
        dictionary of user_id: token
    """
    result: List[Dict[str, Any]] = []
    for user in raw_users:
        user_id, resp = create_user(client, user=user)
        result.append(resp.json())
    return result


def view_own_profile(client: "TestClient", user_id: str) -> "Response":
    """Views profile of the current user on the REST API

    Args:
        client: the fastapi TestClient for testing the app
        user_id: the user ID for authentication

    Returns:
        the REST httpx.Response
    """
    return client.get("/me", headers=create_mss_headers(user_id))


def delete_own_profile(client: "TestClient", user_id: str) -> "Response":
    """Deletes profile of the current user

    Args:
        client: the fastapi TextClient in which the tests run
        user_id: the user ID for authentication

    Returns:
        the httpx.Response
    """
    return client.delete("/me", headers=create_mss_headers(user_id))


def remove_user(
    client: "TestClient",
    current_user_id: str,
    user_id: str,
    is_admin: Optional[bool] = None,
) -> "Response":
    """Delete the user of the given user ID

    Args:
        client: the fastapi TestClient for running tests
        current_user_id: the current user sending this request
        user_id: the unique identifier of the given user
        is_admin: whether to access the user-deletion endpoint as an admin

    Returns:
        the httpx.Response
    """
    headers = create_mss_headers(current_user_id, is_admin=is_admin)
    return client.delete(f"/users/{user_id}", headers=headers)


def view_user_list(
    client: "TestClient",
    user_id: str,
    skip: int = 0,
    limit: Optional[int] = None,
    is_admin: Optional[bool] = None,
) -> "Response":
    """Views the pagination list of users on the REST API

    Args:
        client: the fastapi TestClient for testing the API
        user_id: the user ID for authentication
        skip: the number of records to skip
        limit: the maximum number of records to return
        is_admin: whether to visit the user list endpoint as an admin

    Returns:
        the httpx.Response
    """
    params = {"skip": skip}
    if isinstance(limit, int):
        params["limit"] = limit

    headers = create_mss_headers(user_id, is_admin=is_admin)
    return client.get("/users", headers=headers, params=params)
