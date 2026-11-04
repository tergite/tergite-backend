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
from tergite_tuner.lib.nodes import DEFAULT_NODE_SPECS

from tests.utils.fixtures import load_fixture

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

    element, qois = _get_element_qois(node, session)
    data = {
        "cs": {k: {} for k in qubits + couplers},
        "transmons": {k: {} for k in qubits},
        "couplers": {k: {} for k in couplers},
    }
    elements, group = (
        (qubits, "transmons") if element == "qubit" else (couplers, "couplers")
    )
    for name in elements:
        for qoi in qois:
            data[group][name][qoi] = RECALIBRATION_MOCKS[element].get(qoi)
        data["cs"][name][node.value] = "calibrated"

    session.state.write(data)
    # FIXME: data is not fully nested as expected.
    return session, data


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
    transmon_configuration = session.node_config.root
    if node_name not in transmon_configuration:
        logging.info(f"{node_name} does not have specific node config")
        return
    if is_node_calibrated:
        logging.info(f"{node_name} is already calibrated")
        return
    data = transmon_configuration[node_name]
    couplers = session.couplers
    qubits = session.qubits

    all_components_node_conf = data.get("all", {})
    session.state.load(
        {
            # FIXME: this should probably be only qubits not qubits + couplers
            "transmons": {k: all_components_node_conf for k in qubits},
            "couplers": {
                coupler: data[coupler] for coupler in couplers if coupler in data
            },
        },
    )


def _get_element_qois(
    node: NodeEnum, session: "SessionContext"
) -> tuple[Literal["qubit", "coupler"], list[str]]:
    """Whether the node calibrates qubits or couplers, and the fields it sets"""
    spec = {**DEFAULT_NODE_SPECS, **session.node_specs}[node]
    return spec.element, list(spec.qois.values())
