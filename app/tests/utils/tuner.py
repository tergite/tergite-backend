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
"""Utilities for testing with tergite_tuner library installed."""
import logging
import os
from typing import Any, Dict, Literal, Unpack

from tergite_tuner import NodeEnum, SessionContext
from tergite_tuner.config.session import SessionOptions
from tergite_tuner.lib.base.node import CouplerNode, QubitNode

from app.tests.utils.fixtures import load_fixture

RECALIBRATION_MOCKS: Dict[Literal["qubit", "coupler"], Dict[str, Any]] = load_fixture(
    "recalibration-mocks.json"
)


def mock_run_node(
    node: NodeEnum,
    env_file: str | os.PathLike[str] | None = None,
    session: SessionContext | None = None,
    refresh_session: bool = True,
    keep_data_files: bool = True,
    **options: Unpack[SessionOptions],
) -> Any:
    """A mock run node function"""
    if session is None:
        session = SessionContext.from_env(env_file=env_file, **options)
        _populate_initial_parameters(session)

    _populate_node_parameters(
        node.value,
        is_node_calibrated=False,
        session=session,
    )

    qubits = session.qubits
    couplers = session.couplers

    assert refresh_session is False
    assert keep_data_files is False
    assert qubits == ["q12", "q13", "q14"]
    assert couplers == ["q12_q13", "q12_q14"]

    node_cls = session.node_cls_map[node]
    data = {
        "cs": {k: {} for k in qubits + couplers},
        "transmons": {k: {} for k in qubits},
        "couplers": {k: {} for k in couplers},
    }
    if issubclass(node_cls, QubitNode):
        for q in qubits:
            for qoi in node_cls.qubit_qois or []:
                data["transmons"][q][qoi] = RECALIBRATION_MOCKS["qubit"].get(qoi)
                session._redis_fields_touched[qoi] = (
                    session._redis_fields_touched.get(qoi, 0) + 1
                )
            data["cs"][q][node.value] = "calibrated"
    elif issubclass(node_cls, CouplerNode):
        for c in couplers:
            for qoi in node_cls.coupler_qois or []:
                data["couplers"][c][qoi] = RECALIBRATION_MOCKS["coupler"].get(qoi)
                session._redis_fields_touched[qoi] = (
                    session._redis_fields_touched.get(qoi, 0) + 1
                )
            data["cs"][c][node.value] = "calibrated"

    session.redis_store.save_many(data)
    # FIXME: data is not fully nested as expected.
    return session, data


def _populate_initial_parameters(session: "SessionContext"):
    """Populates the store with the initial values from the device config

    Args:
        session: the session context
    """
    initial_qubit_parameters = session.device_config.qubits
    initial_coupler_parameters = session.device_config.couplers

    session.redis_store.save_many(
        {
            "transmons": {
                qubit: initial_qubit_parameters[qubit]
                for qubit in session.qubits
                if qubit in initial_qubit_parameters
            },
            "couplers": {
                coupler: initial_coupler_parameters[coupler]
                for coupler in session.couplers
                if coupler in initial_coupler_parameters
            },
        }
    )


def _populate_node_parameters(
    node_name: str,
    is_node_calibrated: bool,
    session: "SessionContext",
):
    """Populate the database with node specific parameter values from the node config

    Args:
        session: the session context
        node_name: the name of the node
        is_node_calibrated: whether the node is calibrated
    """
    transmon_configuration = session.node_config
    if not node_name in transmon_configuration:
        logging.info(f"{node_name} does not have specific node config")
        return
    if is_node_calibrated:
        logging.info(f"{node_name} is already calibrated")
        return
    data = transmon_configuration[node_name]
    couplers = session.couplers
    qubits = session.qubits

    all_components_node_conf = data.get("all", {})
    session.redis_store.save_many(
        {
            # FIXME: this should probably be only qubits not qubits + couplers
            "transmons": {k: all_components_node_conf for k in qubits},
            "couplers": {
                coupler: data[coupler] for coupler in couplers if coupler in data
            },
        }
    )
