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
"""Tests for picking the cluster that recalibration runs through"""
import pytest

pytest.importorskip("quantify_scheduler")

from app.libs.quantum_executor.quantify.executor import _get_recalibration_cluster
from app.libs.quantum_executor.quantify.utils.config import QuantifyMetadata

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


def _get_cluster(ip_address: str) -> dict:
    return {"instrument_type": "Cluster", "ip_address": ip_address, "is_dummy": True}
