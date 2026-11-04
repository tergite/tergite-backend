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

import gc
import math
from typing import Any, Dict, Generator, List

import numpy as np
import pytest
import toml

from libs.device_parameters import BackendConfig
from libs.quantum_executor.base import gates
from libs.quantum_executor.base.circuits import QasmCircuit
from libs.quantum_executor.base.qasm import to_native_circuit
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from tests.utils.circuits import get_gate_level_seed, get_qasm
from tests.utils.env import TEST_BACKEND_SETTINGS_FILE, TEST_QUANTIFY_SEED_FILE
from utils.exc import UnsupportedCircuitError

_ELEMENTS = ["q12", "q13", "q14"]
_NATIVE_CONFIG = NativeQobjConfig.from_measurement_settings(
    meas_level=2, meas_return="single", shots=1, n_qubits=3
)
_DRIVE = gates.Frame(kind="drive", qubit_ids=(12,))
_READOUT = gates.Frame(kind="readout", qubit_ids=(12,))
_COUPLER = gates.Frame(kind="coupler", qubit_ids=(12, 13))
_NS = 1e-9


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


@pytest.fixture
def calibrated_backend_config(tmp_path) -> BackendConfig:
    """The test backend config, with the seed the gate-level jobs run on, whose coupler of
    q12 and q13 is calibrated"""
    seed_file = tmp_path / "seed.toml"
    seed_file.write_text(toml.dumps(get_gate_level_seed()))
    return BackendConfig.from_toml(TEST_BACKEND_SETTINGS_FILE, seed_file=str(seed_file))


@pytest.fixture
def calibrated_device(calibrated_backend_config) -> Generator[Any, Any, None]:
    """A quantum device without hardware, of the calibrated backend config"""
    from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

    from libs.quantum_executor.quantify.device import apply_calibration

    device = QuantumDevice("test_pulse_level_circuits")
    apply_calibration(device, calibrated_backend_config)
    yield device
    device.close()


def test_each_shot_starts_after_the_qubits_relax(quantum_device):
    """The first operation of a shot waits INITIAL_RESET_VALUE, so that a shot does not
    start in the state the last one left"""
    from quantify_scheduler.backends.graph_compilation import SerialCompiler

    from libs.quantum_executor.quantify.circuits import (
        INITIAL_RESET_VALUE,
        get_circuit_schedule,
    )

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


@pytest.mark.parametrize(
    "roles, expected",
    [(None, ("control", "target")), (("q13", "q12"), ("target", "control"))],
    ids=["by-name", "given"],
)
def test_a_czs_phase_corrections_go_to_its_qubits_by_role(tmp_path, roles, expected):
    """Each correction is its control's or its target's, as the calibration names them,
    or else as the coupler's name orders its qubits"""
    from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

    from libs.quantum_executor.quantify.device import apply_calibration

    seed = get_gate_level_seed()
    coupler = seed["calibration_config"]["coupler"][0]
    if roles is not None:
        coupler["control_qubit"], coupler["target_qubit"] = roles
    seed_file = tmp_path / "seed.toml"
    seed_file.write_text(toml.dumps(seed))
    config = BackendConfig.from_toml(TEST_BACKEND_SETTINGS_FILE, seed_file=seed_file)
    device = QuantumDevice("test_cz_roles")
    apply_calibration(device, config)
    edge = device.get_edge("q12_q13")

    corrections = {
        "control": math.degrees(coupler["control_rz_lambda"]),
        "target": math.degrees(coupler["target_rz_lambda"]),
    }
    got = (edge.parent_phase_correction(), edge.child_phase_correction())
    device.close()
    assert got == pytest.approx(tuple(corrections[role] for role in expected))


def test_a_device_reapplied_after_its_instruments_closed_keeps_the_new_ones(
    quantum_device,
):
    """QCoDeS holds instruments only weakly, so the device must hold the new ones"""
    from qcodes.instrument import Instrument

    from libs.quantum_executor.quantify.device import apply_calibration

    names = [*quantum_device.elements(), *quantum_device.edges()]
    for name in names:
        Instrument.find_instrument(name).close()

    apply_calibration(
        quantum_device,
        BackendConfig.from_toml(
            TEST_BACKEND_SETTINGS_FILE, seed_file=TEST_QUANTIFY_SEED_FILE
        ),
    )
    gc.collect()

    assert [name for name in names if not Instrument.exist(name)] == []


def test_a_device_reapplied_without_its_couplers_drops_their_edges(quantum_device):
    from libs.quantum_executor.quantify.device import apply_calibration

    backend_config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=TEST_QUANTIFY_SEED_FILE
    )
    without_couplers = backend_config.model_copy(
        update={
            "calibration_config": backend_config.calibration_config.model_copy(
                update={"coupler": []}
            )
        }
    )

    apply_calibration(quantum_device, without_couplers)

    assert list(quantum_device.edges()) == []
    assert list(quantum_device.elements()) == ["q12", "q13", "q14"]


@pytest.mark.parametrize("qubit_id, expected", [(6, "q06"), (12, "q12")])
def test_transmons_are_named_as_the_hardware_config_names_ports(qubit_id, expected):
    from libs.quantum_executor.quantify.device import get_element_name

    assert get_element_name(qubit_id) == expected


def test_a_pulse_block_follows_what_precedes_it_and_runs_its_frames_side_by_side(
    calibrated_device,
):
    block = gates.PulseBlock(
        operations=(
            gates.Play(frame=_DRIVE, samples=(0.1,) * 10),
            gates.Play(frame=_READOUT, samples=(0.2,) * 20),
            gates.Play(frame=_COUPLER, samples=(0.1,) * 4),
        )
    )
    circuit = _get_circuit([gates.Rxy(0, math.pi, 0.0), block])

    got = _get_pulse_times(calibrated_device, circuit)

    rotation_end = got[("Rxy", "q12:mw")] + 56 * _NS
    assert got[("NumericalPulse", "q12:mw")] == pytest.approx(rotation_end)
    assert got[("NumericalPulse", "q12:res")] == pytest.approx(rotation_end)
    assert got[("NumericalPulse", "q12_q13:fl")] == pytest.approx(rotation_end)


def test_operations_of_a_frame_follow_each_other_on_the_time_grid(calibrated_device):
    """A frame's next operation starts on the 4 ns grid after its last ends, and a phase
    update takes a grid interval, as a sequencer takes one update at a time"""
    block = gates.PulseBlock(
        operations=(
            gates.Play(frame=_DRIVE, samples=(0.1,) * 10),
            gates.ShiftPhase(frame=_DRIVE, phase=0.5),
            gates.Play(frame=_READOUT, samples=(0.2,) * 20),
            gates.Capture(frame=_READOUT, duration=16 * _NS, clbit=0, channel=0),
        )
    )

    got = _get_pulse_times(calibrated_device, _get_circuit([block]))

    start = got[("NumericalPulse", "q12:mw")]
    assert got[("ShiftClockPhase", "q12.01")] == pytest.approx(start + 12 * _NS)
    assert got[("SSBIntegrationComplex", "q12:res")] == pytest.approx(start + 20 * _NS)


def test_what_follows_a_pulse_block_starts_once_its_frames_are_done(calibrated_device):
    block = gates.PulseBlock(
        operations=(
            gates.Play(frame=_DRIVE, samples=(0.1,) * 8),
            gates.Play(frame=_READOUT, samples=(0.2,) * 20),
            gates.Capture(frame=_READOUT, duration=16 * _NS, clbit=0, channel=0),
        )
    )
    circuit = _get_circuit([block, gates.Rxy(1, math.pi, 0.0)])

    got = _get_pulse_times(calibrated_device, circuit)

    start = got[("NumericalPulse", "q12:mw")]
    assert got[("Rxy", "q13:mw")] == pytest.approx(start + 36 * _NS)


def test_a_frame_whose_frequency_changes_starts_each_shot_at_its_own(calibrated_device):
    """Each shot runs the program anew, so a frame's frequency a block changes is set to
    its calibrated one as the shot starts"""
    block = gates.PulseBlock(
        operations=(gates.ShiftFrequency(frame=_DRIVE, frequency=-1e6),)
    )
    circuit = _get_circuit([block])

    schedule = _get_schedule(calibrated_device, circuit)

    reset, shift = [
        operation
        for operation in _get_all_operations(schedule)
        if type(operation).__name__ == "SetClockFrequency"
    ]
    drive_frequency = calibrated_device.get_element("q12").clock_freqs.f01()
    assert reset.data["pulse_info"][0]["clock_freq_new"] == drive_frequency
    assert shift.data["pulse_info"][0]["clock_freq_new"] == drive_frequency - 1e6


def test_a_frame_the_device_has_not_is_refused(calibrated_device):
    frame = gates.Frame(kind="drive", qubit_ids=(15,))
    block = gates.PulseBlock(operations=(gates.Play(frame=frame, samples=(0.1,)),))

    with pytest.raises(UnsupportedCircuitError, match="frame q15_drive is not one of"):
        _get_schedule(calibrated_device, _get_circuit([block]))


def test_the_published_frames_are_those_a_job_plays_on(
    calibrated_device, calibrated_backend_config
):
    from libs.quantum_executor.quantify.defcals import get_pulse_calibration
    from libs.quantum_executor.quantify.device import get_frame_frequencies

    frames, _ = get_pulse_calibration(calibrated_backend_config)

    by_name = {frame.name: frame for frame in frames}
    assert set(by_name) == set(get_frame_frequencies(calibrated_device))
    q13_drive = by_name["q13_drive"]
    assert (q13_drive.kind, q13_drive.qubits, q13_drive.frequency) == (
        "drive",
        [1],
        5.2e9,
    )
    assert by_name["q12_q13_coupler"].qubits == [0, 1]
    # its coupler is not calibrated, so its frame has no frequency to play at
    assert "q12_q14_coupler" not in by_name


@pytest.mark.parametrize(
    "statement",
    ["x $1;", "sx $1;", "rz(0.3) $1;", "cz $0, $1;", "cz $1, $0;", "cz $0, $2;"],
)
def test_a_published_defcal_plays_what_its_gate_plays(
    calibrated_device, calibrated_backend_config, statement
):
    from libs.quantum_executor.quantify.defcals import get_pulse_calibration

    _, defcals = get_pulse_calibration(calibrated_backend_config)
    programs = "\n".join(defcal.program for defcal in defcals)

    by_gate = _get_played(calibrated_device, _translate(statement))
    by_defcal = _get_played(
        calibrated_device,
        _translate(f'defcalgrammar "openpulse";\n{programs}\n{statement}'),
    )

    assert by_gate
    assert [event[:2] for event in by_defcal] == [event[:2] for event in by_gate]
    for (*_, defcal_values), (*_, gate_values) in zip(by_defcal, by_gate):
        np.testing.assert_allclose(defcal_values, gate_values, rtol=0, atol=1e-12)


def test_a_published_defcal_replaces_its_gate(calibrated_backend_config):
    from libs.quantum_executor.quantify.defcals import get_pulse_calibration

    _, defcals = get_pulse_calibration(calibrated_backend_config)
    programs = "\n".join(defcal.program for defcal in defcals)

    circuit = _translate(f'defcalgrammar "openpulse";\n{programs}\nx $1;\ncz $0, $1;')

    assert all(isinstance(gate, gates.PulseBlock) for gate in circuit.gates)


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
    from libs.quantum_executor.quantify.circuits import INITIAL_RESET_VALUE

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


def _get_schedule(quantum_device, circuit: gates.NativeCircuit) -> Any:
    """Gets the circuit's schedule, its pulse-level blocks on the device's frames"""
    from libs.quantum_executor.quantify.circuits import (
        DeviceFrames,
        get_circuit_schedule,
    )
    from libs.quantum_executor.quantify.device import get_frame_frequencies

    frames = DeviceFrames(dt=_NS, frequencies=get_frame_frequencies(quantum_device))
    return get_circuit_schedule(
        circuit, _NATIVE_CONFIG, **_get_kwargs(quantum_device), frames=frames
    )


def _get_pulse_times(quantum_device, circuit: gates.NativeCircuit) -> Dict[Any, float]:
    """Gets when each operation of the circuit's compiled schedule starts, in seconds, by
    its kind, as the timing table writes it, and its port, or its clock where it has no
    port"""
    from quantify_scheduler.backends.graph_compilation import SerialCompiler

    compiled = SerialCompiler("test").compile(
        _get_schedule(quantum_device, circuit),
        config=quantum_device.generate_compilation_config(),
    )
    return {
        (str(row.operation).split("(")[0], row.port or row.clock): row.abs_time
        for _, row in compiled.timing_table.data.iterrows()
    }


def _translate(statements: str) -> gates.NativeCircuit:
    """Translates the statements, on the physical qubits of the test device, into native
    gates"""
    qasm = get_qasm(statements, num_clbits=0)
    return to_native_circuit(QasmCircuit(name="test", qasm=qasm, num_clbits=0), dt=_NS)


def _get_played(quantum_device, circuit: gates.NativeCircuit) -> List[Any]:
    """Gets what the circuit's compiled schedule plays, in time order: each waveform's
    samples and each phase shift, with the nanosecond it starts at and its port, or its
    clock where it has none"""
    from quantify_scheduler.backends.graph_compilation import SerialCompiler
    from quantify_scheduler.helpers.waveforms import get_waveform

    compiled = SerialCompiler("test").compile(
        _get_schedule(quantum_device, circuit),
        config=quantum_device.generate_compilation_config(),
    )
    played = []
    for start, pulse in _get_timed_pulses(compiled, 0.0):
        if pulse.get("wf_func") is not None and pulse.get("port"):
            samples = get_waveform(pulse, sampling_rate=1 / _NS)
            played.append((round(start / _NS), pulse["port"], samples))
        elif "phase_shift" in pulse:
            played.append((round(start / _NS), pulse["clock"], [pulse["phase_shift"]]))
    return sorted(played, key=lambda event: event[:2])


def _get_timed_pulses(schedule, offset: float) -> List[Any]:
    """Gets each pulse of the compiled schedule, those within it too, with its start in
    seconds"""
    timed_pulses = []
    for schedulable in schedule.schedulables.values():
        start = offset + schedulable["abs_time"]
        operation = schedule.operations[schedulable["operation_id"]]
        if hasattr(operation, "schedulables"):
            timed_pulses.extend(_get_timed_pulses(operation, start))
            continue
        for pulse in operation.data.get("pulse_info", []):
            timed_pulses.append((start + pulse.get("t0", 0.0), pulse))
    return timed_pulses


def _get_all_operations(schedule) -> List[Any]:
    """Gets the schedule's operations, those of the schedules within it too"""
    operations = []
    for operation in schedule.operations.values():
        if hasattr(operation, "operations"):
            operations.extend(_get_all_operations(operation))
        else:
            operations.append(operation)
    return operations
