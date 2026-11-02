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
"""Tests of the quantify schedules of gate-level circuits"""
import math
from typing import Any, Dict, Generator, List

import pytest

from libs.device_parameters import BackendConfig
from libs.quantum_executor.base import gates
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from tests.utils.env import TEST_BACKEND_SETTINGS_FILE, TEST_QUANTIFY_SEED_FILE
from tests.utils.params import HAS_QUANTIFY
from utils.exc import UnsupportedCircuitError

pytestmark = pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")

_ELEMENTS = ["q12", "q13", "q14"]
_NATIVE_CONFIG = NativeQobjConfig.from_measurement_settings(
    meas_level=2, meas_return="single", shots=1, n_qubits=3
)


@pytest.fixture
def quantum_device() -> Generator[Any, Any, None]:
    """A quantum device without hardware, its transmons and edges from the test seed"""
    from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

    from libs.quantum_executor.quantify.device import apply_calibration

    device = QuantumDevice("test_gate_level_circuits")
    backend_config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=TEST_QUANTIFY_SEED_FILE
    )
    apply_calibration(device, backend_config)
    yield device
    device.close()


def test_each_shot_starts_after_the_qubits_relax(quantum_device):
    """The first operation of a shot waits as long as the qobj path's first ones do, so
    that a shot does not start in the state the last one left"""
    from quantify_scheduler.backends.graph_compilation import SerialCompiler

    from libs.quantum_executor.quantify.circuits import get_circuit_schedule
    from libs.quantum_executor.quantify.instruction import INITIAL_RESET_VALUE

    circuit = _get_circuit([gates.Measure(qubit=0, clbit=0, channel=0)])
    schedule = get_circuit_schedule(
        circuit, _NATIVE_CONFIG, **_get_kwargs(quantum_device)
    )
    compiled = SerialCompiler("test").compile(
        schedule, config=quantum_device.generate_compilation_config()
    )

    idle, measure = list(schedule.schedulables)
    assert compiled.schedulables[measure]["abs_time"] == INITIAL_RESET_VALUE


def test_consecutive_measurements_of_distinct_qubits_start_together(quantum_device):
    circuit = _get_circuit(
        [
            gates.Rxy(0, math.pi, 0.0),
            gates.Measure(qubit=0, clbit=1, channel=0),
            gates.Measure(qubit=1, clbit=0, channel=1),
            gates.Rxy(1, math.pi, 0.0),
        ]
    )

    _, first_measure, second_measure, after = _get_start_times(quantum_device, circuit)

    measure_duration = _get_measure_duration(quantum_device, "q12")
    assert first_measure == second_measure
    assert after == pytest.approx(first_measure + measure_duration)


def test_measurements_of_one_qubit_follow_each_other(quantum_device):
    circuit = _get_circuit(
        [
            gates.Measure(qubit=0, clbit=0, channel=0),
            gates.Measure(qubit=0, clbit=1, channel=1),
        ]
    )

    got = _get_start_times(quantum_device, circuit)

    measure_duration = _get_measure_duration(quantum_device, "q12")
    assert got == pytest.approx([0.0, measure_duration])


def test_consecutive_delays_of_distinct_qubits_start_together(quantum_device):
    """qiskit splits a delay of several qubits into one per qubit, which idle together"""
    circuit = _get_circuit(
        [
            gates.Delay(qubits=(0,), duration=1e-6),
            gates.Delay(qubits=(1,), duration=2e-6),
            gates.Rxy(0, math.pi, 0.0),
        ]
    )

    first_delay, second_delay, after = _get_start_times(quantum_device, circuit)

    assert first_delay == second_delay == 0.0
    assert after == pytest.approx(2e-6)


def test_delays_of_one_qubit_follow_each_other(quantum_device):
    circuit = _get_circuit(
        [
            gates.Delay(qubits=(0,), duration=1e-6),
            gates.Delay(qubits=(0,), duration=1e-6),
        ]
    )

    assert _get_start_times(quantum_device, circuit) == pytest.approx([0.0, 1e-6])


def test_measurements_are_acquired_on_their_channels(quantum_device):
    from libs.quantum_executor.quantify.circuits import get_circuit_schedule

    circuit = _get_circuit(
        [
            gates.Measure(qubit=2, clbit=0, channel=0),
            gates.Measure(qubit=0, clbit=1, channel=1),
        ]
    )

    schedule = get_circuit_schedule(
        circuit, _NATIVE_CONFIG, **_get_kwargs(quantum_device)
    )

    got = {
        operation.data["gate_info"]["acq_channel_override"]: operation.data[
            "gate_info"
        ]["device_elements"]
        for operation in _get_gate_operations(schedule)
    }
    assert got == {0: ["q14"], 1: ["q12"]}


@pytest.mark.parametrize("control, target", [(0, 1), (1, 0)])
def test_cz_runs_on_the_coupler_of_its_qubits_in_either_order(
    quantum_device, control, target
):
    from libs.quantum_executor.quantify.circuits import get_circuit_schedule

    circuit = _get_circuit([gates.CZ(control=control, target=target)])

    schedule = get_circuit_schedule(
        circuit, _NATIVE_CONFIG, **_get_kwargs(quantum_device)
    )

    [operation] = _get_gate_operations(schedule)
    assert operation.data["gate_info"]["device_elements"] == ["q12", "q13"]


def test_cz_of_uncoupled_qubits_is_refused(quantum_device):
    from libs.quantum_executor.quantify.circuits import get_circuit_schedule

    circuit = _get_circuit([gates.CZ(control=1, target=2)])

    with pytest.raises(UnsupportedCircuitError, match="q13 and q14 have no coupler"):
        get_circuit_schedule(circuit, _NATIVE_CONFIG, **_get_kwargs(quantum_device))


def test_circuit_wider_than_the_device_is_refused(quantum_device):
    from libs.quantum_executor.quantify.circuits import get_circuit_schedule

    circuit = gates.NativeCircuit(name="wide", num_qubits=4, num_clbits=0, gates=())

    with pytest.raises(UnsupportedCircuitError, match="has 4 qubits"):
        get_circuit_schedule(circuit, _NATIVE_CONFIG, **_get_kwargs(quantum_device))


@pytest.mark.parametrize("qubit_id, expected", [(6, "q06"), (12, "q12")])
def test_transmons_are_named_as_the_hardware_config_names_ports(qubit_id, expected):
    from libs.quantum_executor.quantify.device import get_element_name

    assert get_element_name(qubit_id) == expected


def _get_circuit(native_gates: List[gates.Gate]) -> gates.NativeCircuit:
    """Gets a circuit of the gates on the three qubits of the test device"""
    return gates.NativeCircuit(
        name="test", num_qubits=3, num_clbits=2, gates=tuple(native_gates)
    )


def _get_kwargs(quantum_device) -> Dict[str, Any]:
    """Gets the device's arguments of get_circuit_schedule"""
    from libs.quantum_executor.quantify.device import get_edge_names

    return {
        "element_names": _ELEMENTS,
        "edge_names": get_edge_names(quantum_device),
        "measure_durations": {
            name: _get_measure_duration(quantum_device, name) for name in _ELEMENTS
        },
    }


def _get_measure_duration(quantum_device, element: str) -> float:
    """Gets the seconds a measurement of the device's transmon takes"""
    from libs.quantum_executor.quantify.circuits import get_measure_duration

    measure = quantum_device.get_element(element).measure
    return get_measure_duration(
        measure.pulse_duration(), measure.acq_delay(), measure.integration_time()
    )


def _get_start_times(quantum_device, circuit: gates.NativeCircuit) -> List[float]:
    """Gets when each operation of the circuit's compiled schedule starts, in the order
    they were added, from the end of the idle each shot starts with"""
    from quantify_scheduler.backends.graph_compilation import SerialCompiler

    from libs.quantum_executor.quantify.circuits import get_circuit_schedule

    schedule = get_circuit_schedule(
        circuit, _NATIVE_CONFIG, **_get_kwargs(quantum_device)
    )
    compiled = SerialCompiler("test").compile(
        schedule, config=quantum_device.generate_compilation_config()
    )
    from libs.quantum_executor.quantify.instruction import INITIAL_RESET_VALUE

    _, *times = [
        compiled.schedulables[label]["abs_time"] for label in schedule.schedulables
    ]
    return [time - INITIAL_RESET_VALUE for time in times]


def _get_gate_operations(schedule) -> List[Any]:
    """Gets the schedule's operations but the idle each shot starts with"""
    from quantify_scheduler.operations.pulse_library import IdlePulse

    return [
        operation
        for operation in schedule.operations.values()
        if not isinstance(operation, IdlePulse)
    ]
