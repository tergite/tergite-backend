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
"""Tests that the committed OpenAPI schema is the app's"""

import json
import re

from api import app
from dev.openapi.export import SCHEMA_FILE, render_schema


def test_committed_schema_matches_the_app():
    assert SCHEMA_FILE.read_text() == render_schema(app.openapi()), (
        f"{SCHEMA_FILE.name} is stale: run python -m dev.openapi.export"
    )


def test_schema_holds_the_events_the_backend_sends_mss():
    """MSS generates its models of the websocket's events and its replies from these"""
    schemas = app.openapi()["components"]["schemas"]

    event_data = {
        ref["$ref"].rsplit("/", 1)[-1] for ref in schemas["DeviceEventData"]["anyOf"]
    }

    assert event_data == {"Device", "DeviceCalibration", "Job", "DeviceSwitchData"}
    assert {"DeviceEvent", "EventResponse"} <= set(schemas)


def test_no_event_schema_allows_undeclared_fields():
    """MSS on SQL refuses a field it does not declare, as it did the device's duplicates"""
    schemas = app.openapi()["components"]["schemas"]
    pending, seen = ["DeviceEvent"], set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        text = json.dumps(schemas[name])
        pending.extend(re.findall(r"#/components/schemas/([\w-]+)", text))

    allowing = [name for name in seen if schemas[name].get("additionalProperties")]

    assert allowing == []
