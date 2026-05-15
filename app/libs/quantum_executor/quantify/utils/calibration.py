# This code is part of Tergite
#
# (C) Chalmers Next Labs (2026)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Utils for running recalibration"""
import enum
from typing import Literal, Mapping, Optional, Unpack

from tergite_tuner import NodeEnum, SessionContext, read_result, run_node
from tergite_tuner.config.session import SessionOptions

from app.libs.device_parameters import BackendConfig, DeviceCalibration
from app.utils.datetime import utc_now_str

_CalibrationResult = Mapping[
    Literal["transmons", "couplers", "cs"],
    Mapping[str, Mapping[str, str | float | int | bool | list | dict | None]],
]


class _RunMode(enum.Enum):
    ALL_QUBITS = 0
    ONE_QUBIT_AT_A_TIME = 1


# the order of the nodes matters also
_QUBITS_NODE_RUN_MAP = {
    NodeEnum.RABI_OSCILLATIONS: _RunMode.ALL_QUBITS,
    NodeEnum.RAMSEY_CORRECTION: _RunMode.ALL_QUBITS,
    NodeEnum.RAMSEY_CORRECTION_12: _RunMode.ONE_QUBIT_AT_A_TIME,
    NodeEnum.RO_FREQUENCY_TWO_STATE_OPTIMIZATION: _RunMode.ALL_QUBITS,
    NodeEnum.RO_AMPLITUDE_TWO_STATE_OPTIMIZATION: _RunMode.ALL_QUBITS,
    NodeEnum.RO_FREQUENCY_THREE_STATE_OPTIMIZATION: _RunMode.ALL_QUBITS,
    NodeEnum.RO_AMPLITUDE_THREE_STATE_OPTIMIZATION: _RunMode.ALL_QUBITS,
    NodeEnum.RANDOMIZED_BENCHMARKING: _RunMode.ALL_QUBITS,
}

_FIXED_DURATION_COUPLER_NODES = (
    NodeEnum.CZ_CHEVRON,
    NodeEnum.CZ_CALIBRATION,
    NodeEnum.CZ_LOCAL_PHASES,
    NodeEnum.CZ_RB,
)
_NON_FIXED_DURATION_COUPLER_NODES = (
    NodeEnum.CZ_CALIBRATION,
    NodeEnum.CZ_LOCAL_PHASES,
    NodeEnum.CZ_RB,
)


def recalibrate(
    backend_config: BackendConfig, **session_options: Unpack[SessionOptions]
) -> DeviceCalibration:
    """Recalibrate the entire device and updates the calibration seed file

    Args:
        backend_config: The backend configuration
        session_options: the options for creating the session

    Returns:
        the new device calibration
    """
    session = _recalibrate_all_qubits(**session_options)
    session = _recalibrate_all_couplers(session)
    results = read_result(session)
    coupler_index_map = {
        f"{q1}_{q2}": int(k.lstrip("u"))
        for k, (q1, q2) in backend_config.device_config.coupling_dict.items()
    }
    parsed_result = DeviceCalibration.from_calib_results(
        results,
        name_id_map=coupler_index_map,
        reverse_phase_qubits=session.reverse_phase_qubits,
        name=backend_config.general_config.name,
        version=backend_config.general_config.version,
        last_calibrated=utc_now_str(),
    )
    return parsed_result


def _recalibrate_all_qubits(
    **session_options: Unpack[SessionOptions],
) -> SessionContext:
    """Recalibrates only the qubits passed, ignoring the couplers

    Args:
        session_options: the options for setting up the session

    Returns:
        the updated session object and the results
    """
    qubits: list = session_options.get("qubits", [])
    session: Optional[SessionContext] = None
    for node, run_mode in _QUBITS_NODE_RUN_MAP.items():
        if run_mode == _RunMode.ALL_QUBITS:
            session, _ = run_node(
                session=session,
                qubits=qubits,
                node=node,
                refresh_session=False,
                keep_data_files=False,
                **session_options,
            )
        elif run_mode == _RunMode.ONE_QUBIT_AT_A_TIME:
            for qubit in qubits:
                session, _ = run_node(
                    session=session,
                    qubits=[qubit],
                    node=node,
                    refresh_session=False,
                    keep_data_files=False,
                    **session_options,
                )
        else:
            raise RuntimeError(f"Unknown run mode: {run_mode}")

    return session


def _recalibrate_all_couplers(session: SessionContext) -> SessionContext:
    """Recalibrates only the couplers in session

    Args:
        session: the session context

    Returns:
        the updated session context
    """
    couplers: list = session.couplers
    fixed_duration_couplers: tuple = session.fixed_duration_couplers

    for coupler in couplers:
        is_fixed_duration = coupler in fixed_duration_couplers
        session = _recalibrate_one_coupler(
            coupler, is_fixed_duration=is_fixed_duration, session=session
        )

    return session


def _recalibrate_one_coupler(
    coupler: str, is_fixed_duration: bool, session: SessionContext
) -> SessionContext:
    """Recalibrates the coupler given the session

    Args:
        coupler: the coupler name
        is_fixed_duration: whether the coupler's duration is fixed
        session: the session context

    Returns:
        the updated session context
    """
    qubits = coupler.split("_")
    nodes = (
        _FIXED_DURATION_COUPLER_NODES
        if is_fixed_duration
        else _NON_FIXED_DURATION_COUPLER_NODES
    )
    for node in nodes:
        session, _ = run_node(
            session=session,
            qubits=qubits,
            couplers=[coupler],
            node=node,
            refresh_session=False,
            keep_data_files=False,
        )

    return session
