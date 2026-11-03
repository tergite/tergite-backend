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
"""Tests of the simulated device that plays quantify's pulses on QuTiP"""
import math
from typing import Any, Generator, List

import numpy as np
import pytest
import toml

from libs.device_parameters import BackendConfig
from libs.quantum_executor.base import gates
from libs.quantum_executor.base.circuits import QasmCircuit
from libs.quantum_executor.base.qasm import to_native_circuit
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from tests.utils.circuits import get_gate_level_seed, get_qasm, get_unitary
from tests.utils.env import TEST_BACKEND_SETTINGS_FILE

_SHOTS = 4000
_ELEMENTS = ["q12", "q13", "q14"]
_READOUT_IQ = {
    "q12": [[1, 0], [-1, 0]],
    "q13": [[0, 1], [0, -1]],
    "q14": [[2, 2], [-2, -2]],
}
# the gates' error on a three-level transmon without DRAG, and the shots' spread
_TOLERANCE = 0.06


@pytest.fixture
def simulated_config(tmp_path) -> BackendConfig:
    """The test backend config with the gate-level seed, its coupler of q12 and q13
    calibrated, and a simulator section"""
    seed_file = tmp_path / "seed.toml"
    seed_file.write_text(toml.dumps(get_gate_level_seed()))
    config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=str(seed_file)
    )
    simulator = {"readout_iq": _READOUT_IQ, "readout_noise": 0.05}
    return BackendConfig.model_validate({**config.model_dump(), "simulator": simulator})


@pytest.fixture
def simulated(simulated_config) -> Generator[Any, Any, None]:
    """The quantum device of the simulated config, without hardware, and its simulated
    device"""
    from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

    from libs.quantum_executor.quantify.device import apply_calibration
    from libs.quantum_executor.quantify.simulator import get_simulated_device

    device = QuantumDevice("test_simulator")
    apply_calibration(device, simulated_config)
    yield device, get_simulated_device(device, simulated_config)
    device.close()


@pytest.mark.parametrize(
    "body",
    [
        "x $0;",
        "sx $0;",
        "rx(0.9) $0;",
        "sx $0;\nrz(pi) $0;\nsx $0;",
        # each of these reads otherwise if a clock's phase turns its pulses the other way
        "ry(0.8) $0;\nrz(1.1) $0;\nsx $0;",
        "sx $0;\nrz(pi/2) $0;\nry(pi/2) $0;",
        "h $0;\ns $0;\nsx $0;",
    ],
)
def test_one_qubit_gates_read_out_as_their_unitaries(simulated, body):
    got = _get_ones(simulated, body, num_qubits=1)

    assert got == pytest.approx(_get_ideal_ones(body, num_qubits=1), abs=_TOLERANCE)


@pytest.mark.parametrize(
    "body",
    [
        "h $0;\ncx $0, $1;",
        "x $0;\nh $1;\ncz $0, $1;\nh $1;",
        "h $1;\ncz $0, $1;\nh $1;",
    ],
    ids=["bell", "cz-flips-when-control-is-1", "cz-idles-when-control-is-0"],
)
def test_a_calibrated_cz_reads_out_as_its_unitary(simulated, body):
    got = _get_ones(simulated, body, num_qubits=2)

    assert got == pytest.approx(_get_ideal_ones(body, num_qubits=2), abs=_TOLERANCE)


def test_bell_outcomes_are_correlated(simulated):
    dataset = _run(simulated, "h $0;\ncx $0, $1;", num_qubits=2)

    first, second = (
        _get_bits(dataset[channel], _READOUT_IQ[name])
        for channel, name in [(0, "q12"), (1, "q13")]
    )
    assert np.mean(first == second) > 0.95


def test_a_coupler_without_a_calibrated_pulse_does_not_entangle(simulated):
    _, device = simulated

    assert "q12_q13:fl" in device.zz_rates
    assert "q12_q14:fl" not in device.zz_rates


def test_an_excited_qubit_relaxes_at_its_t1(simulated):
    _, device = simulated
    wait = 20e-6

    [got] = _get_ones(simulated, "x $0;\ndelay[20us] $0;", num_qubits=1)

    assert got == pytest.approx(
        math.exp(-wait / device.qubits["q12"].t1), abs=_TOLERANCE
    )


@pytest.mark.parametrize("amplitude", [0.02, 0.05, 0.08])
def test_a_cal_block_drives_at_the_rabi_rate(simulated, amplitude):
    _, device = simulated
    duration = 40e-9
    body = (
        'defcalgrammar "openpulse";\n'
        f"cal {{\n    play(q12_drive, constant({amplitude}, 40ns));\n}}"
    )

    [got] = _get_ones(simulated, body, num_qubits=1)

    angle = 2 * math.pi * device.qubits["q12"].rabi_rate * amplitude * duration
    assert got == pytest.approx(math.sin(angle / 2) ** 2, abs=_TOLERANCE)


def test_a_gate_on_another_qubit_after_a_measurement_runs(simulated):
    body = "c[0] = measure $0;\nx $1;\nc[1] = measure $1;"

    got = _get_ones(simulated, body, num_qubits=2, should_measure_all=False)

    assert got == pytest.approx([0, 1], abs=_TOLERANCE)


def test_a_gate_on_a_measured_qubit_is_refused(simulated):
    from utils.exc import UnsupportedCircuitError

    with pytest.raises(UnsupportedCircuitError, match="measures each qubit once"):
        body = "c[0] = measure $0;\nx $0;"
        _run(simulated, body, num_qubits=1, should_measure_all=False)


def test_appended_readout_has_each_shots_iq_point(simulated):
    dataset = _run(simulated, "x $0;", num_qubits=2)

    assert dataset[0].dims == ("repetition", "acq_index_0")
    assert dataset[0].shape == (_SHOTS, 1)
    assert dataset[1].shape == (_SHOTS, 1)


def test_averaged_readout_has_the_mean_iq_point(simulated):
    dataset = _run(simulated, "x $0;", num_qubits=1, meas_return="avg")

    assert dataset[0].dims == ("acq_index_0",)
    # q12 excited reads near its state 1 point, -1 + 0j
    assert complex(dataset[0].values[0]) == pytest.approx(-1, abs=0.1)


def test_the_discriminators_split_each_qubits_two_points(simulated):
    from libs.quantum_executor.quantify.simulator import get_discriminators

    _, device = simulated

    discriminators = get_discriminators(device)

    for name, (point0, point1) in _READOUT_IQ.items():
        lda = discriminators[name]
        scores = [
            lda["coef_0"] * i + lda["coef_1"] * q + lda["intercept"]
            for i, q in (point0, point1)
        ]
        assert scores[0] < 0 < scores[1]


def _get_ones(
    simulated, body: str, num_qubits: int, should_measure_all: bool = True
) -> List[float]:
    """Gets each qubit's share of shots read as 1"""
    dataset = _run(simulated, body, num_qubits, should_measure_all=should_measure_all)
    return [
        float(np.mean(_get_bits(dataset[index], _READOUT_IQ[_ELEMENTS[index]])))
        for index in range(num_qubits)
    ]


def _run(
    simulated,
    body: str,
    num_qubits: int,
    meas_return: str = "single",
    should_measure_all: bool = True,
):
    """Runs the statements on the simulated device, each qubit measured into its bit at
    the end unless told otherwise"""
    from quantify_scheduler.backends.graph_compilation import SerialCompiler

    from libs.quantum_executor.quantify.circuits import (
        DeviceFrames,
        get_circuit_schedule,
        get_measure_duration,
    )
    from libs.quantum_executor.quantify.device import (
        get_edge_names,
        get_frame_frequencies,
    )
    from libs.quantum_executor.quantify.simulator import run_schedule

    device, simulated_device = simulated
    measures = "".join(f"\nc[{i}] = measure ${i};" for i in range(num_qubits))
    if not should_measure_all:
        measures = ""
    qasm = get_qasm(body + measures, num_clbits=num_qubits)
    circuit = to_native_circuit(
        QasmCircuit(name="test", qasm=qasm, num_clbits=num_qubits), dt=1e-9
    )
    native_config = NativeQobjConfig.from_measurement_settings(
        meas_level=1, meas_return=meas_return, shots=_SHOTS, n_qubits=3
    )
    measure_durations = {}
    for name in _ELEMENTS:
        measure = device.get_element(name).measure
        measure_durations[name] = get_measure_duration(
            pulse_duration=measure.pulse_duration(),
            acq_delay=measure.acq_delay(),
            integration_time=measure.integration_time(),
        )
    schedule = get_circuit_schedule(
        circuit,
        native_config,
        element_names=_ELEMENTS,
        edge_names=get_edge_names(device),
        measure_durations=measure_durations,
        frames=DeviceFrames(dt=1e-9, frequencies=get_frame_frequencies(device)),
    )
    compiled = SerialCompiler("test").compile(
        schedule, config=device.generate_compilation_config()
    )
    return run_schedule(compiled, simulated_device, _SHOTS, np.random.default_rng(7))


def _get_bits(values, points) -> np.ndarray:
    """Gets each shot's bit: 1 where its IQ point is nearer the qubit's state 1 point"""
    point0, point1 = (complex(*point) for point in points)
    values = np.asarray(values).ravel()
    return (np.abs(values - point1) < np.abs(values - point0)).astype(int)


def _get_ideal_ones(body: str, num_qubits: int) -> List[float]:
    """Gets each qubit's probability of 1 after the statements' ideal unitary on |0...0>"""
    qasm = get_qasm(body, num_clbits=0)
    circuit = to_native_circuit(
        QasmCircuit(name="ideal", qasm=qasm, num_clbits=0), dt=1e-9
    )
    native = [gate for gate in circuit.gates if not isinstance(gate, gates.Delay)]
    probabilities = np.abs(get_unitary(native, num_qubits)[:, 0]) ** 2
    return [
        sum(p for index, p in enumerate(probabilities) if (index >> qubit) & 1)
        for qubit in range(num_qubits)
    ]
