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
"""Gives each request an id, and logs it"""
from typing import Optional

from fastapi import Request
from pydantic import ConfigDict, Field

import settings

from ..utils.datetime import utc_now_str
from ..utils.redis import get_redis_connection
from ..utils.redis_store import Collection, Schema
from ..utils.strings import uuid_str

_REQUEST_LOGS_STORES: dict[str, Collection["RequestLog"]] = {}


class RequestLog(Schema):
    """Schema for tracking requests"""

    __primary_key_fields__ = ("request_id",)
    __index_fields__ = (
        "user_id",
        "ip_address",
    )
    model_config = ConfigDict(extra="allow")

    request_id: str
    user_id: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: Optional[str] = Field(default_factory=utc_now_str)
    updated_at: Optional[str] = Field(default_factory=utc_now_str)

    @classmethod
    def from_request(cls, request: Request) -> "RequestLog":
        """Generates a request log object from the request

        Args:
            request: the received HTTP request

        Returns:
            the request log object
        """
        user_id = request.headers.get("x-mss-user-id")
        request_id = request.state.request_id
        return cls(
            request_id=request_id,
            user_id=user_id,
            ip_address=f"{request.client.host}",
        )


async def log_requests(request: Request, call_next):
    """Adds an `x-request-id` header and logs requests received

    It will get it from `x-mss-request-id` if that is present or generate a new one

    Args:
        request: the current FastAPI request object
        call_next: the callback that calls the next middleware or route handler
    """
    request_id = request.headers.get("x-mss-request-id")
    if request_id is None:
        request_id = uuid_str()

    request.state.request_id = request_id

    response = await call_next(request)

    requests_store = get_request_logs_store()
    if not requests_store.exists(request_id):
        requests_store.insert(RequestLog.from_request(request))

    response.headers["X-Request-ID"] = request_id
    return response


def get_request_logs_store() -> Collection[RequestLog]:
    """Gets the current device's store for the request logs

    Returns:
        the RedisCollection containing the request logs
    """
    request_logs_store = _REQUEST_LOGS_STORES.get(settings.DEFAULT_PREFIX)
    if request_logs_store is None:
        request_logs_store = Collection(
            connection=get_redis_connection(url=settings.RQ_REDIS_URL),
            schema=RequestLog,
            default_ttl=settings.REQUEST_LOG_TTL,
            cleanup_interval=settings.REQUEST_LOG_CLEAN_INTERVAL,
        )
        _REQUEST_LOGS_STORES[settings.DEFAULT_PREFIX] = request_logs_store

    return request_logs_store
