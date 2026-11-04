# This code is part of Tergite
#
# (C) Martin Ahindura 2023, 2024
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
"""Tests for the API's device property routes and what start-up sends MSS"""
import pytest

import settings
from api.worker import preload_worker_globals
from services.scheduler import close_device_resources
from services.scheduler.dispatcher import Dispatcher
from tests.utils.api import (
    create_mss_headers,
)
from tests.utils.params import (
    DYNAMIC_PROPERTIES_PARAMS,
    FASTAPI_CLIENTS,
    STATIC_PROPERTIES_PARAMS,
    USERS,
)
from tests.utils.records import remove_dates
from tests.utils.users import (
    create_user,
)

# what each test device publishes for pulse-level jobs: the quantify one sets pulse limits,
# its couplers have no calibrated frequency, so no frames, but their cz's phase shifts; the
# simulators take no pulse-level jobs
PULSE_CALIBRATIONS = {
    "system_test": {
        "frames": [
            ("q12_drive", "drive", [0]),
            ("q12_readout", "readout", [0]),
            ("q13_drive", "drive", [1]),
            ("q13_readout", "readout", [1]),
            ("q14_drive", "drive", [2]),
            ("q14_readout", "readout", [2]),
        ],
        "defcals": [
            *((gate, [qubit]) for qubit in range(3) for gate in ("x", "sx", "rz")),
            ("cz", [0, 1]),
            ("cz", [1, 0]),
            ("cz", [0, 2]),
            ("cz", [2, 0]),
        ],
    },
    "sim_1q": {"frames": None, "defcals": None},
    "sim_2q": {"frames": None, "defcals": None},
}


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_only_the_heavy_worker_publishes_the_device_and_its_calibration(
    client, mock_mss
):
    """The API's start-up publishes neither, so that it never overwrites what the heavy
    worker, which alone has the executor's calibration, has published"""
    with client as client:
        response = client.get("/static-properties", headers=create_mss_headers())
        assert response.status_code == 404
        assert mock_mss.sent_events == []

        device = preload_worker_globals(
            settings.get_device_settings(), should_load_executor=True
        )
        try:
            static_properties = client.get(
                "/static-properties", headers=create_mss_headers()
            ).json()
            dynamic_properties = client.get(
                "/dynamic-properties", headers=create_mss_headers()
            ).json()
        finally:
            close_device_resources(device)

        [initialized, recalibrated] = mock_mss.sent_events[:2]
        assert initialized["name"] == "initialized"
        assert initialized["data"] == static_properties
        assert recalibrated["name"] == "recalibrated"
        assert recalibrated["data"] == dynamic_properties


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_the_heavy_worker_publishes_the_frames_and_defcals_of_pulse_level_jobs(
    client, mock_mss
):
    with client as client:
        close_device_resources(
            preload_worker_globals(
                settings.get_device_settings(), should_load_executor=True
            )
        )

    [recalibrated] = [
        event for event in mock_mss.sent_events if event["name"] == "recalibrated"
    ]
    calibration = recalibrated["data"]
    expected = PULSE_CALIBRATIONS[calibration["name"]]
    if expected["frames"] is None:
        assert (calibration["frames"], calibration["defcals"]) == (None, None)
        return
    frames = [(v["name"], v["kind"], v["qubits"]) for v in calibration["frames"]]
    defcals = [(v["gate"], v["qubits"]) for v in calibration["defcals"]]
    assert frames == expected["frames"]
    assert defcals == expected["defcals"]
    assert all(v["frequency"] > 0 for v in calibration["frames"])
    assert all(v["program"].startswith("defcal ") for v in calibration["defcals"])


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_the_heavy_worker_starts_without_mss_and_publishes_once_it_is_back(
    client, mock_mss
):
    with client as client:
        mock_mss.is_unreachable = True
        device = preload_worker_globals(
            settings.get_device_settings(), should_load_executor=True
        )
        try:
            mock_mss.is_unreachable = False
            light_worker = Dispatcher(
                {device.settings.default_prefix: device},
                should_run_heavy=False,
                should_run_light=True,
            )
            light_worker.run(is_burst=True)
        finally:
            close_device_resources(device)

        event_names = [event["name"] for event in mock_mss.sent_events]
        assert event_names[:2] == ["initialized", "recalibrated"]


@pytest.mark.parametrize("client, expected", STATIC_PROPERTIES_PARAMS)
def test_get_static_properties(client, expected):
    """Get to '/static-properties' retrieves the current static properties of the backend"""
    # using context manager to ensure on_startup runs
    with client as client:
        # the heavy worker publishes the device's properties once the API has started
        close_device_resources(
            preload_worker_globals(
                settings.get_device_settings(), should_load_executor=True
            )
        )
        user_id, _ = create_user(client, USERS[0])
        headers = create_mss_headers(user_id)

        response = client.get("/static-properties", headers=headers)
        got = response.json()
        assert response.status_code == 200
        assert got == expected


@pytest.mark.parametrize("client, _expected", STATIC_PROPERTIES_PARAMS)
def test_unauthenticated_get_static_props(client, _expected):
    """Get to '/static-properties' outside MSS fails with 401"""
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/static-properties")
        got = response.json()
        assert response.status_code == 401
        assert got == {"detail": "user not authenticated"}


@pytest.mark.parametrize("client, expected", DYNAMIC_PROPERTIES_PARAMS)
def test_get_dynamic_properties(client, expected):
    """Get to '/dynamic-properties' retrieves the calibrated device parameters"""
    # using context manager to ensure on_startup runs
    with client as client:
        # the heavy worker publishes its executor's calibration, e.g. the simulators'
        # trained discriminators, once the API has started
        close_device_resources(
            preload_worker_globals(
                settings.get_device_settings(), should_load_executor=True
            )
        )
        user_id, _ = create_user(client, USERS[0])
        headers = create_mss_headers(user_id)

        response = client.get("/dynamic-properties", headers=headers)
        got = response.json()
        assert response.status_code == 200
        # what a device publishes for pulse-level jobs has its own test
        for key in ("frames", "defcals"):
            got.pop(key)
        assert remove_dates(got) == expected


@pytest.mark.parametrize("client, _expected", STATIC_PROPERTIES_PARAMS)
def test_unauthenticated_get_dynamic_props(client, _expected):
    """Get to '/dynamic-properties' outside MSS fails with 401"""
    # using context manager to ensure on_startup runs
    with client as client:
        response = client.get("/dynamic-properties")
        got = response.json()
        assert response.status_code == 401
        assert got == {"detail": "user not authenticated"}
