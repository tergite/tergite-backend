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
"""Tests of the translation of OpenPulse cal blocks and defcals into pulse operations"""

import math

import numpy as np
import pytest

from libs.quantum_executor.base import gates
from libs.quantum_executor.base.circuits import Measurement, QasmCircuit
from libs.quantum_executor.base.qasm import to_native_circuit
from tests.utils.circuits import get_qasm
from utils.exc import UnsupportedCircuitError

_DT = 1e-9
_DRIVE = gates.Frame(kind="drive", qubit_ids=(12,))
_READOUT = gates.Frame(kind="readout", qubit_ids=(12,))
_COUPLER = gates.Frame(kind="coupler", qubit_ids=(12, 13))
# the middle of each of the sample times of 40 ns
_TIMES = (np.arange(40) + 0.5) * _DT
_D = 40e-9

# (waveform, its samples by its definition)
_WAVEFORMS = [
    ("constant(0.2, 40ns)", np.full(40, 0.2)),
    (
        "gaussian(0.5, 40ns, 10ns)",
        0.5 * np.exp(-((_TIMES - _D / 2) ** 2) / (2 * 1e-8**2)),
    ),
    ("sech(0.5im, 40ns, 10ns)", 0.5j / np.cosh((_TIMES - _D / 2) / 1e-8)),
    (
        "gaussian_square(0.4, 40ns, 20ns, 4ns)",
        0.4
        * np.exp(-(np.maximum(np.abs(_TIMES - _D / 2) - 1e-8, 0) ** 2) / (2 * 4e-9**2)),
    ),
    (
        "drag(0.5, 40ns, 10ns, 2e-9)",
        0.5
        * np.exp(-((_TIMES - _D / 2) ** 2) / (2 * 1e-8**2))
        * (1 - 1j * 2e-9 * (_TIMES - _D / 2) / 1e-8**2),
    ),
    (
        "sine(0.3, 40ns, 50e6, pi/2)",
        0.3 * np.sin(2 * math.pi * 50e6 * _TIMES + math.pi / 2),
    ),
]


@pytest.mark.parametrize("waveform, expected", _WAVEFORMS, ids=lambda w: str(w)[:10])
def test_waveform_generators_are_sampled_at_the_middle_of_each_sample_time(
    waveform, expected
):
    [block] = _translate(f"cal {{ play(q12_drive, {waveform}); }}").gates

    [play] = block.operations
    assert play.frame == _DRIVE
    np.testing.assert_allclose(play.samples, expected, atol=1e-12)


def test_waveforms_may_be_declared_or_be_arrays_of_samples():
    native = _translate(
        "cal {\n"
        "    waveform pulse = constant(0.1, 3dt);\n"
        "    play(q12_drive, pulse);\n"
        "    waveform samples = {0.1, 0.2im, -0.3};\n"
        "    play(q12_q13_coupler, samples);\n"
        "}"
    )

    [block] = native.gates
    assert block.operations == (
        gates.Play(frame=_DRIVE, samples=(0.1, 0.1, 0.1)),
        gates.Play(frame=_COUPLER, samples=(0.1, 0.2j, -0.3)),
    )


def test_frame_instructions_translate_to_pulse_operations():
    native = _translate(
        "cal {\n"
        "    shift_phase(q12_drive, pi/2);\n"
        "    set_phase(q12_drive, 0.1);\n"
        "    shift_frequency(q12_readout, -1e6);\n"
        "    set_frequency(q12_readout, 7.1e9);\n"
        "    delay[100ns] q12_drive, q12_readout;\n"
        "    barrier q12_drive, q12_readout;\n"
        "    barrier;\n"
        "}"
    )

    [block] = native.gates
    assert block.operations == (
        gates.ShiftPhase(frame=_DRIVE, phase=math.pi / 2),
        gates.SetPhase(frame=_DRIVE, phase=0.1),
        gates.ShiftFrequency(frame=_READOUT, frequency=-1e6),
        gates.SetFrequency(frame=_READOUT, frequency=7.1e9),
        gates.FrameDelay(frame=_DRIVE, duration=100 * 1e-9),
        gates.FrameDelay(frame=_READOUT, duration=100 * 1e-9),
        gates.FrameBarrier(frames=(_DRIVE, _READOUT)),
        gates.FrameBarrier(frames=()),
    )


def test_a_capture_is_acquired_into_its_bit_on_the_next_channel():
    """A capture goes to the classical bit it is assigned to, on the channel after the
    measurements before it, and is read out as a measurement of its frame's qubit"""
    native = _translate(
        "c[1] = measure $0;\n"
        "cal {\n"
        "    play(q13_readout, constant(0.2, 1us));\n"
        "    c[0] = capture_v1(q13_readout, 800ns);\n"
        "}",
        num_clbits=2,
    )

    measure, block = native.gates
    readout_frame = gates.Frame(kind="readout", qubit_ids=(13,))
    assert measure == gates.Measure(qubit=0, clbit=1, channel=0)
    assert block.operations[1] == gates.Capture(
        frame=readout_frame, duration=800 * 1e-9, clbit=0, channel=1
    )
    assert native.to_readout(qubit_ids=[12, 13]).measurements == (
        Measurement(qubit_id=12, clbit=1),
        Measurement(qubit_id=13, clbit=0),
    )


def test_a_defcal_replaces_the_gate_on_its_qubits_alone():
    native = _translate(
        "defcal x $0 { play(q12_drive, constant(0.3, 4dt)); }\nx $0;\nx $1;"
    )

    block, rotation = native.gates
    assert block == gates.PulseBlock(
        operations=(gates.Play(frame=_DRIVE, samples=(0.3,) * 4),)
    )
    assert rotation == gates.Rxy(1, math.pi, 0.0)


def test_a_defcal_takes_its_parameters_by_name():
    native = _translate(
        "defcal rx(angle[20] theta) $0 { play(q12_drive, constant(theta / pi, 4dt)); }\n"
        "rx(pi/2) $0;"
    )

    [block] = native.gates
    assert block.operations[0].samples == pytest.approx((0.5,) * 4)


def test_a_defcal_of_a_number_matches_that_parameter_alone():
    native = _translate(
        "defcal rz(pi/2) $0 { shift_phase(q12_drive, -pi/2); }\n"
        "rz(pi/2) $0;\n"
        "rz(0.3) $0;"
    )

    block, rotation = native.gates
    assert block.operations == (gates.ShiftPhase(frame=_DRIVE, phase=-math.pi / 2),)
    assert rotation == gates.Rz(0, 0.3)


@pytest.mark.parametrize(
    "statements, message",
    [
        ("cal { play(q12_flux, constant(0.1, 4dt)); }", "q12_flux is not a frame"),
        (
            "cal { frame f = newframe(q12_drive, 5e9, 0); }",
            "frames other than the device's are not supported",
        ),
        ("cal { play(q12_drive, constant(1.5, 4dt)); }", "magnitude beyond 1"),
        ("cal { play(q12_drive, triangle(0.1, 4dt)); }", "waveform triangle is not"),
        ("cal { extern foo(duration) -> waveform; }", "extern foo is not supported"),
        ("cal { capture_v1(q12_readout, 1us); }", "keeps no result is not supported"),
        ("cal { c[0] = capture_v1(q12_drive, 1us); }", "takes a readout frame"),
        ("qubit q;\ndefcal x q { }", "of other than physical qubits"),
        (
            "defcal measure $0 -> bit { }",
            "a defcal with a result, as of measure, is not supported",
        ),
    ],
    ids=[
        "unknown-frame",
        "newframe",
        "magnitude",
        "unknown-waveform",
        "unknown-extern",
        "capture-without-bit",
        "capture-of-drive",
        "defcal-of-declared-qubit",
        "defcal-with-result",
    ],
)
def test_what_has_no_pulse_operations_is_refused(statements, message):
    with pytest.raises(UnsupportedCircuitError, match=message):
        _translate(statements, num_clbits=1)


def test_a_calibration_grammar_other_than_openpulse_is_refused():
    circuit = QasmCircuit(
        name="test",
        qasm='OPENQASM 3.0;\ndefcalgrammar "other";\nqubit q;',
        num_clbits=0,
    )

    with pytest.raises(UnsupportedCircuitError, match="calibration grammar other"):
        to_native_circuit(circuit, dt=_DT)


def _translate(statements: str, num_clbits: int = 0) -> gates.NativeCircuit:
    """Translates the statements, after a defcalgrammar of openpulse"""
    qasm = get_qasm(f'defcalgrammar "openpulse";\n{statements}', num_clbits)
    circuit = QasmCircuit(name="test", qasm=qasm, num_clbits=num_clbits)
    return to_native_circuit(circuit, dt=_DT)
