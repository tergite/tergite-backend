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
"""Tests of the limits a device's pulse-level jobs are kept to"""
import pytest

from libs.device_parameters import BackendConfig
from libs.device_parameters.dtos import PulseLimits
from libs.quantum_executor.base import gates
from libs.quantum_executor.base.circuits import QasmCircuit
from libs.quantum_executor.base.pulse_limits import (
    check_pulse_limits,
    get_calibrated_frame_frequencies,
)
from libs.quantum_executor.base.qasm import to_native_circuit
from tests.utils.circuits import get_qasm
from tests.utils.env import TEST_BACKEND_SETTINGS_FILE, TEST_QUANTIFY_SEED_FILE
from utils.exc import UnsupportedCircuitError

_DT = 1e-9
_LIMITS = PulseLimits(
    max_amplitude={"drive": 0.5, "readout": 0.4},
    max_frequency_offset=10e6,
    max_energy_per_shot={"drive": 1e-8, "readout": 1e-7},
    max_energy_per_job={"drive": 1e-6, "readout": 1e-5},
    max_pulse_duration=2e-6,
    max_samples_per_waveform=1000,
    max_capture_duration=1e-6,
)
_FREQUENCIES = {"q12_drive": 5.0e9, "q12_readout": 7.0e9, "q12_q13_coupler": 4.0e9}


def test_a_job_without_pulses_is_not_checked():
    """A gate-level job runs on a device whatever its limits, even none"""
    circuit = _get_native_circuit("x $0;\nc[0] = measure $0;")

    check_pulse_limits([circuit], shots=10, limits=None, frame_frequencies={}, dt=_DT)


def test_pulses_within_the_limits_pass():
    circuit = _get_native_circuit(
        "cal {\n"
        "    play(q12_drive, gaussian(0.4, 40ns, 10ns));\n"
        "    shift_frequency(q12_drive, -5e6);\n"
        "    play(q12_readout, constant(0.3, 500ns));\n"
        "    c[0] = capture_v1(q12_readout, 400ns);\n"
        "}"
    )

    _check([circuit], shots=10)


@pytest.mark.parametrize(
    "pulses, message",
    [
        (
            "play(q12_drive, constant(0.6, 4dt));",
            "the amplitude of a waveform on q12_drive, 0.6, is beyond the device's "
            "limit of 0.5",
        ),
        (
            "play(q12_q13_coupler, constant(0.1, 4dt));",
            "coupler frames may not be played on this device",
        ),
        (
            "set_frequency(q12_drive, 5.02e9);",
            "moving q12_drive from its calibrated frequency, 20 MHz, is beyond the "
            "device's limit of 10 MHz",
        ),
        (
            "shift_frequency(q12_drive, 6e6);\nshift_frequency(q12_drive, 6e6);",
            "moving q12_drive from its calibrated frequency, 12 MHz, is beyond",
        ),
        (
            "play(q12_drive, constant(0.1, 1100dt));",
            "the samples of a waveform on q12_drive, 1100, is beyond the device's "
            "limit of 1000",
        ),
        (
            "c[0] = capture_v1(q12_readout, 1.5us);",
            "a capture on q12_readout, 1.5 us, is beyond the device's limit of 1 us",
        ),
        (
            "play(q12_readout, constant(0.4, 800ns));\n"
            "play(q12_readout, constant(0.4, 800ns));",
            "the energy on q12_readout in a shot, 2.56e-07, is beyond the device's "
            "limit of 1e-07",
        ),
        (
            "delay[3us] q12_drive;",
            "the pulse-level blocks of a shot, 3 us, is beyond the device's limit of 2 us",
        ),
        (
            "play(q14_drive, constant(0.1, 4dt));",
            "frame q14_drive is not one of the device's",
        ),
    ],
    ids=[
        "amplitude",
        "kind",
        "set-frequency",
        "shifted-frequency",
        "samples",
        "capture",
        "energy-per-shot",
        "duration",
        "uncalibrated-frame",
    ],
)
def test_pulses_beyond_a_limit_are_refused(pulses, message):
    circuit = _get_native_circuit(f"cal {{\n{pulses}\n}}")

    with pytest.raises(UnsupportedCircuitError, match=message):
        _check([circuit], shots=10)


def test_the_energy_over_a_job_counts_each_shot_of_each_circuit():
    """A drive of 0.3 for 100 ns is 9e-9 a shot, within the limit of a shot, but its 60
    shots in each of two circuits are beyond the job's"""
    circuit = _get_native_circuit("cal { play(q12_drive, constant(0.3, 100ns)); }")

    with pytest.raises(
        UnsupportedCircuitError,
        match="the energy on q12_drive over the job, 1.08e-06, is beyond the "
        "device's limit of 1e-06",
    ):
        _check([circuit, circuit], shots=60)


def test_a_device_without_limits_takes_no_pulse_level_jobs():
    circuit = _get_native_circuit("cal { play(q12_drive, constant(0.1, 4dt)); }")

    with pytest.raises(
        UnsupportedCircuitError,
        match="the device takes no pulse-level jobs: its operators have set no "
        "pulse limits",
    ):
        check_pulse_limits(
            [circuit], shots=1, limits=None, frame_frequencies=_FREQUENCIES, dt=_DT
        )


def test_the_calibrated_frequencies_are_named_after_the_frames():
    backend_config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=TEST_QUANTIFY_SEED_FILE
    )

    got = get_calibrated_frame_frequencies(backend_config)

    assert {"q12_drive", "q12_readout", "q13_drive", "q12_q13_coupler"} <= set(got)
    qubit = next(q for q in backend_config.calibration_config.qubit if q["id"] == "q12")
    assert got["q12_drive"] == qubit["frequency"]


def test_a_device_without_a_calibration_has_no_calibrated_frequencies():
    backend_config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=TEST_QUANTIFY_SEED_FILE
    ).model_copy(update={"calibration_config": None})

    assert get_calibrated_frame_frequencies(backend_config) == {}


def _check(circuits, shots: int) -> None:
    """Checks the circuits against the test limits"""
    check_pulse_limits(
        circuits,
        shots=shots,
        limits=_LIMITS,
        frame_frequencies=_FREQUENCIES,
        dt=_DT,
    )


def _get_native_circuit(statements: str) -> gates.NativeCircuit:
    """Translates the statements, after a defcalgrammar of openpulse, with one bit"""
    qasm = get_qasm(f'defcalgrammar "openpulse";\n{statements}', num_clbits=1)
    return to_native_circuit(QasmCircuit(name="test", qasm=qasm, num_clbits=1), dt=_DT)
