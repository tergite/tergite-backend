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
"""Routes each request to the device its path names, e.g. /Loke/bookings to Loke's /bookings"""
from starlette.types import ASGIApp, Receive, Scope, Send

import settings


class DeviceRoutingMiddleware:
    """Makes the device that a request's first path segment names the current one; a
    request that names none is the default device's

    The segment moves into the root path, as Starlette's Mount does, so that the routes
    match the rest of the path and the URLs built from the request keep the segment.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        root_path = scope.get("root_path", "")
        route_path = scope["path"].removeprefix(root_path)
        device = route_path.split("/")[1] if route_path.startswith("/") else ""
        if device not in settings.get_device_names():
            await self.app(scope, receive, send)
            return

        with settings.using_device(device):
            await self.app(
                {**scope, "root_path": f"{root_path}/{device}"}, receive, send
            )
