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
"""Tests for the cluster and SPI rack recalibration runs through, and the couplers' roles"""
import pytest

pytest.importorskip("quantify_scheduler")

from tergite_tuner.utils.types.enums import SPIMode

from libs.quantum_executor.quantify.executor import (
    _get_recalibration_cluster,
    _get_spi_options,
)
from libs.quantum_executor.quantify.utils.calibration import _get_coupler_roles
from libs.quantum_executor.quantify.utils.config import QuantifyMetadata
from tests.utils.env import TEST_CALIBRATION_DEVICE_CONFIG_FILE
from tests.utils.fixtures import get_fixture_path

_SPI_RACK = {"instrument_type": "SPI-Rack", "port": "/dev/ttyACM0"}


def test_recalibration_runs_through_the_one_cluster():
    metadata = QuantifyMetadata.model_validate(
        {"cluster0": _get_cluster("192.0.2.1"), "spi_rack": _SPI_RACK}
    )

    name, conf = _get_recalibration_cluster(metadata)

    assert (name, conf.ip_address) == ("cluster0", "192.0.2.1")


def test_recalibration_without_a_cluster_runs_through_none():
    metadata = QuantifyMetadata.model_validate({"spi_rack": _SPI_RACK})

    assert _get_recalibration_cluster(metadata) is None


def test_recalibration_through_two_clusters_is_refused():
    metadata = QuantifyMetadata.model_validate(
        {"cluster0": _get_cluster("192.0.2.1"), "cluster1": _get_cluster("192.0.2.2")}
    )

    with pytest.raises(ValueError, match="lists 2"):
        _get_recalibration_cluster(metadata)


def test_a_couplers_roles_are_those_the_tuners_device_config_gives():
    """tergite-tuner reads them there, and labels the CZ's phase corrections by them"""
    roles = _get_coupler_roles(get_fixture_path("chip_calib_device_config.toml"))

    assert roles == {"q12_q13": ("q13", "q12")}


def test_a_coupler_the_tuners_device_config_gives_no_roles_is_left_out():
    roles = _get_coupler_roles(TEST_CALIBRATION_DEVICE_CONFIG_FILE)

    assert "q12_q13" not in roles
    assert roles["q06_q07"] == ("q07", "q06")


@pytest.mark.parametrize(
    "spi_rack, expected",
    [
        (None, {"spi_mode": SPIMode.dummy}),
        ({**_SPI_RACK, "is_dummy": True}, {"spi_mode": SPIMode.dummy}),
        (
            {**_SPI_RACK, "is_dummy": False},
            {"spi_mode": SPIMode.real, "spi_serial_port": "/dev/ttyACM0"},
        ),
    ],
    ids=["none", "dummy", "real"],
)
def test_the_tuner_sets_currents_on_the_spi_rack_of_the_metadata(spi_rack, expected):
    """A real rack on its port; a dummy, or none, sets no real current"""
    instruments = {"cluster0": _get_cluster("192.0.2.1")}
    if spi_rack is not None:
        instruments["spi_rack"] = spi_rack
    metadata = QuantifyMetadata.model_validate(instruments)

    assert _get_spi_options(metadata) == expected


def _get_cluster(ip_address: str) -> dict:
    return {"instrument_type": "Cluster", "ip_address": ip_address, "is_dummy": True}
