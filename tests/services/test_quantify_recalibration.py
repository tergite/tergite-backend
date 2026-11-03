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
"""Tests for picking the cluster that recalibration runs through, and the nodes it runs"""
import pytest

pytest.importorskip("quantify_scheduler")

from libs.quantum_executor.quantify.executor import _get_recalibration_cluster
from libs.quantum_executor.quantify.utils.calibration import _recalibrate_all_qubits
from libs.quantum_executor.quantify.utils.config import QuantifyMetadata

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


def test_a_node_of_one_qubit_at_a_time_runs_without_the_couplers(mocker):
    """tergite-tuner builds every coupler into a node's device, and fails to compile one
    whose qubits it lacks"""
    run_node = mocker.patch(
        "libs.quantum_executor.quantify.utils.calibration.run_node",
        return_value=(mocker.MagicMock(), None),
    )

    _recalibrate_all_qubits(qubits=["q12", "q13"], couplers=["q12_q13"])

    alone = [
        call for call in run_node.call_args_list if len(call.kwargs["qubits"]) == 1
    ]
    together = [call for call in run_node.call_args_list if call not in alone]
    assert alone and all(call.kwargs["couplers"] == [] for call in alone)
    assert all(call.kwargs["couplers"] == ["q12_q13"] for call in together)


def _get_cluster(ip_address: str) -> dict:
    return {"instrument_type": "Cluster", "ip_address": ip_address, "is_dummy": True}
