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
"""Tests of the simulated chip that plays schedules compiled for the clusters"""
import itertools
import math

import numpy as np
import pytest
from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.enums import BinMode
from quantify_scheduler.operations.control_flow_library import LoopOperation
from quantify_scheduler.operations.gate_library import X90, Measure, Reset, Rxy, X
from quantify_scheduler.operations.pulse_library import (
    IdlePulse,
    SetClockFrequency,
    ShiftClockPhase,
    SoftSquarePulse,
    SquarePulse,
)
from quantify_scheduler.resources import ClockResource
from quantify_scheduler.waveforms import soft_square

from libs.quantum_executor.quantify.simulated_chip.coupler import (
    DOWNCONVERSION_FREQUENCY,
    PAIR_STATES,
    Coupler,
)
from libs.quantum_executor.quantify.simulated_chip.engine import SimulatedChip
from libs.quantum_executor.quantify.simulated_chip.readout import get_transmission
from tests.utils.chips import get_chip_config, get_clusters_device
from utils.exc import UnsupportedCircuitError

_CHIP = get_chip_config()
_QUBIT = _CHIP.qubits["q12"]
_RESONATOR = _CHIP.resonators["q12"]
_REPETITIONS = 1000
_PAIR_REPETITIONS = 400
_COUPLER = _CHIP.couplers["q12_q13"]
_CZ_AMPLITUDE = 0.5
_STATE_11 = PAIR_STATES.index((1, 1))
_STATE_02 = PAIR_STATES.index((0, 2))
# a chip whose shots tell |1> from |2> apart
_QUIET_CHIP = _CHIP.model_copy(update={"readout_noise": 2e-4})


@pytest.fixture(scope="module")
def device(tmp_path_factory):
    """The quantum device of the gate-level seed on the dummy clusters"""
    device = get_clusters_device("test_simulated_chip", tmp_path_factory.mktemp("chip"))
    yield device
    device.close()


@pytest.fixture
def chip() -> SimulatedChip:
    return SimulatedChip(_CHIP, np.random.default_rng(5))


@pytest.mark.parametrize("turns", [0.25, 0.5, 0.8])
def test_a_resonant_pulse_rotates_at_the_chips_rabi_frequency(device, chip, turns):
    duration = 100e-9
    amplitude = turns / (_QUBIT.rabi_frequency * duration)
    schedule = _get_schedule()
    schedule.add(SetClockFrequency(clock="q12.01", clock_freq_new=_QUBIT.frequency))
    schedule.add(
        SquarePulse(amp=amplitude, duration=duration, port="q12:mw", clock="q12.01")
    )
    schedule.add(Measure("q12"))

    excited = _get_excited(device, chip, schedule)

    assert excited == pytest.approx(math.sin(math.pi * turns) ** 2, abs=0.01)


def test_spectroscopy_finds_the_chips_frequency_not_the_calibrations(device, chip):
    calibrated = device.get_element("q12").clock_freqs.f01()
    frequencies = calibrated + np.linspace(-1e6, 1e6, 41)
    duration = 4e-6
    # a pi pulse on resonance, so that the line peaks there, not on its side lobes
    amplitude = 0.5 / (_QUBIT.rabi_frequency * duration)
    schedule = _get_schedule()
    for index, frequency in enumerate(frequencies):
        schedule.add(Reset("q12"))
        schedule.add(SetClockFrequency(clock="q12.01", clock_freq_new=frequency))
        schedule.add(
            SquarePulse(amp=amplitude, duration=duration, port="q12:mw", clock="q12.01")
        )
        schedule.add(Measure("q12", acq_index=index))

    excited = _get_excited(device, chip, schedule)

    assert frequencies[np.argmax(excited)] == pytest.approx(_QUBIT.frequency, abs=60e3)
    assert abs(calibrated - _QUBIT.frequency) > 100e3


def test_ramsey_fringes_beat_at_the_clocks_detuning(device, chip):
    detuning, delays = 2e6, np.arange(64) * 40e-9
    schedule = _get_schedule()
    for index, delay in enumerate(delays):
        schedule.add(Reset("q12"))
        schedule.add(
            SetClockFrequency(
                clock="q12.01", clock_freq_new=_QUBIT.frequency + detuning
            )
        )
        schedule.add(X90("q12"))
        schedule.add(X90("q12"), rel_time=delay)
        schedule.add(Measure("q12", acq_index=index))

    excited = _get_excited(device, chip, schedule)

    spectrum = np.abs(np.fft.rfft(excited - excited.mean(), n=4096))
    beat = np.fft.rfftfreq(4096, d=40e-9)[np.argmax(spectrum)]
    assert beat == pytest.approx(detuning, rel=0.03)


@pytest.mark.parametrize("phase_shift, expected", [(0, 1.0), (90, 0.5), (180, 0.0)])
def test_a_clocks_phase_shift_turns_its_later_pulses(
    device, chip, phase_shift, expected
):
    schedule = _get_schedule()
    schedule.add(SetClockFrequency(clock="q12.01", clock_freq_new=_QUBIT.frequency))
    schedule.add(X90("q12"))
    schedule.add(ShiftClockPhase(phase_shift=phase_shift, clock="q12.01"))
    schedule.add(X90("q12"))
    schedule.add(Measure("q12"))

    assert _get_excited(device, chip, schedule) == pytest.approx(expected, abs=0.05)


def test_a_pulses_phase_advance_undoes_its_clocks_detuning(device, chip):
    """As tergite-tuner's Ramsey nodes take it to, emulating a detuning by the phase of
    their second pulse"""
    delay = 248e-9
    # a quarter turn between the pulses, which a phase advance the wrong way doubles
    turns = 0.25
    detuning = turns / (delay + device.get_element("q12").rxy.duration())
    schedule = _get_schedule()
    schedule.add(
        SetClockFrequency(clock="q12.01", clock_freq_new=_QUBIT.frequency + detuning)
    )
    schedule.add(X90("q12"))
    schedule.add(Rxy(90, -360 * turns, "q12"), rel_time=delay)
    schedule.add(Measure("q12"))

    assert _get_excited(device, chip, schedule) == pytest.approx(1.0, abs=0.03)


def test_a_clocks_phase_shift_turns_later_pulses_against_their_phase(device, chip):
    """As a virtual Z of quantify's gates does: Rz(90) then Rxy(90, 90) rotates about x"""
    schedule = _get_schedule()
    schedule.add(SetClockFrequency(clock="q12.01", clock_freq_new=_QUBIT.frequency))
    schedule.add(X90("q12"))
    schedule.add(ShiftClockPhase(phase_shift=90, clock="q12.01"))
    schedule.add(Rxy(90, 90, "q12"))
    schedule.add(Measure("q12"))

    assert _get_excited(device, chip, schedule) == pytest.approx(1.0, abs=0.03)


def test_an_excited_transmon_relaxes_at_its_t1(device, chip):
    delay = 30e-6
    schedule = _get_schedule()
    schedule.add(SetClockFrequency(clock="q12.01", clock_freq_new=_QUBIT.frequency))
    schedule.add(Rxy(180, 0, "q12"))
    schedule.add(IdlePulse(delay))
    schedule.add(Measure("q12"))

    excited = _get_excited(device, chip, schedule)

    assert excited == pytest.approx(math.exp(-delay / _QUBIT.t1), abs=0.02)


@pytest.mark.parametrize("level", [0, 1])
def test_the_resonators_dip_shifts_with_its_transmons_level(device, chip, level):
    frequencies = _RESONATOR.frequency + np.linspace(-1.5e6, 1e6, 51)
    schedule = _get_schedule()
    for index, frequency in enumerate(frequencies):
        schedule.add(Reset("q12"))
        schedule.add(SetClockFrequency(clock="q12.ro", clock_freq_new=frequency))
        if level:
            schedule.add(
                SetClockFrequency(clock="q12.01", clock_freq_new=_QUBIT.frequency)
            )
            schedule.add(X("q12"))
        schedule.add(Measure("q12", acq_index=index))

    values = _play(device, chip, schedule)[0].values

    amplitude = device.get_element("q12").measure.pulse_amp()
    dip = frequencies[np.argmin(np.abs(values))]
    expected = _RESONATOR.frequency + level * _RESONATOR.dispersive_shift
    # pulled towards the bare frequency by the readout's amplitude
    pull = 1 / (1 + (amplitude / _RESONATOR.critical_amplitude) ** 2)
    expected = _RESONATOR.bare_frequency + (expected - _RESONATOR.bare_frequency) * pull
    assert dip == pytest.approx(expected, abs=60e3)


def test_appended_shots_run_in_order_each_loop_iteration_too(device, chip):
    body = Schedule("body")
    body.add(Reset("q12"))
    body.add(SetClockFrequency(clock="q12.01", clock_freq_new=_QUBIT.frequency))
    body.add(X("q12"))
    body.add(Measure("q12", acq_index=0, bin_mode=BinMode.APPEND))
    body.add(Reset("q12"))
    body.add(Measure("q12", acq_index=1, bin_mode=BinMode.APPEND))
    schedule = Schedule("looped", repetitions=3)
    schedule.add(LoopOperation(body=body, repetitions=5))

    acquired = _play(device, chip, schedule)[0]

    assert acquired.bin_mode == BinMode.APPEND
    assert acquired.indices == [0, 1] * 5
    excited = _to_excited(device, acquired.values).reshape(15, 2)
    assert (excited[:, 0] > 0.5).all() and (excited[:, 1] < 0.5).all()


def test_shots_spread_with_the_chips_readout_noise(device, chip):
    schedule = Schedule("shots", repetitions=4000)
    schedule.add(Reset("q12"))
    schedule.add(Measure("q12", bin_mode=BinMode.APPEND))

    values = _play(device, chip, schedule)[0].values

    integration = device.get_element("q12").measure.integration_time()
    expected = _CHIP.readout_noise * math.sqrt(1e-6 / integration)
    assert np.std(values.real) == pytest.approx(expected, rel=0.05)
    assert np.mean(values) == pytest.approx(_get_response(device, 0), abs=1e-4)


def test_a_transmon_the_chip_lacks_is_refused(device):
    qubits = {name: qubit for name, qubit in _CHIP.qubits.items() if name != "q14"}
    chip = SimulatedChip(_CHIP.model_copy(update={"qubits": qubits}))
    schedule = _get_schedule()
    schedule.add(X("q14"))

    with pytest.raises(ValueError, match="no transmon q14"):
        _play(device, chip, schedule)


def test_a_pulse_on_a_read_transmon_before_its_reset_is_refused(device, chip):
    schedule = _get_schedule()
    schedule.add(Measure("q12"))
    schedule.add(X("q12"))
    schedule.add(Measure("q12", acq_index=1))

    with pytest.raises(UnsupportedCircuitError, match="once between its resets"):
        _play(device, chip, schedule)


def test_a_couplers_flux_pulse_at_their_difference_swaps_11_with_02_and_back(device):
    chip = SimulatedChip(_QUIET_CHIP, np.random.default_rng(5))
    durations = np.arange(24, 328, 16) * 1e-9
    schedule = _get_pair_schedule()
    for index, duration in enumerate(durations):
        _add_pair_preparation(schedule, is_control_excited=True)
        _add_flux_pulse(schedule, duration)
        _add_pair_measurement(schedule, index)

    levels = _get_pair_levels(device, _play(device, chip, schedule), len(durations))

    swapped = np.mean(np.all(levels == (0, 2), axis=-1), axis=0)
    returned = np.mean(np.all(levels == (1, 1), axis=-1), axis=0)
    assert swapped.max() > 0.9
    assert returned[np.argmax(swapped) :].max() > 0.9
    assert returned[0] > 0.9


def test_a_full_swap_through_02_adds_a_conditional_phase_of_pi(device):
    chip = SimulatedChip(_QUIET_CHIP, np.random.default_rng(5))
    duration = _get_full_swap_duration()
    phases = np.arange(8) * 45
    points = list(itertools.product((False, True), phases))
    schedule = _get_pair_schedule()
    for index, (is_control_excited, phase) in enumerate(points):
        _add_pair_preparation(schedule, is_control_excited, is_target_rotated=True)
        _add_flux_pulse(schedule, duration)
        schedule.add(Rxy(90, phase, "q12"))
        schedule.add(Measure("q12", acq_index=index))

    excited = _get_excited(device, chip, schedule).reshape(2, len(phases))

    fringes = (excited - 0.5) @ np.exp(-1j * np.radians(phases))
    conditional = np.degrees(np.angle(fringes[1] / fringes[0])) % 360
    assert conditional == pytest.approx(180, abs=15)


def _get_schedule() -> Schedule:
    """Gets a schedule of the test's repetitions, from a reset of q12"""
    schedule = Schedule("test", repetitions=_REPETITIONS)
    schedule.add(Reset("q12"))
    return schedule


def _play(device, chip: SimulatedChip, schedule: Schedule):
    """Compiles the schedule for the dummy clusters and plays it on the chip"""
    compiled = SerialCompiler("test").compile(
        schedule, config=device.generate_compilation_config()
    )
    return chip.run(compiled)


def _get_excited(device, chip: SimulatedChip, schedule: Schedule) -> np.ndarray:
    """Plays the schedule and gets the share of q12 in |1> at each of its averaged
    acquisitions"""
    return _to_excited(device, _play(device, chip, schedule)[0].values)


def _to_excited(device, values: np.ndarray) -> np.ndarray:
    """Gets where each value lies between q12's responses of |0> and |1>, 0 at |0>"""
    ground, excited = _get_response(device, 0), _get_response(device, 1)
    span = excited - ground
    return np.real((values - ground) * np.conj(span)) / abs(span) ** 2


def _get_response(device, level: int, element: str = "q12") -> complex:
    """Gets what a transmon's calibrated measurement integrates with it in the level:
    its pulse's amplitude times the resonator's transmission, over the share of the
    integration the pulse fills"""
    measure = device.get_element(element).measure
    frequency = device.get_element(element).clock_freqs.readout()
    amplitude = measure.pulse_amp()
    filled = (
        measure.pulse_duration() - measure.acq_delay()
    ) / measure.integration_time()
    transmission = get_transmission(
        _CHIP.resonators[element], np.array(frequency), level, np.array(amplitude)
    )
    return complex(amplitude * transmission * filled)


def _get_pair_schedule() -> Schedule:
    """Gets a schedule of shots of q12 and q13, with the clock of their coupler's flux
    pulse resonant with |11> to |02>"""
    schedule = Schedule("pair", repetitions=_PAIR_REPETITIONS)
    frequency = DOWNCONVERSION_FREQUENCY - _get_resonance()
    schedule.add_resource(ClockResource("q12_q13.cz", frequency))
    return schedule


def _add_pair_preparation(
    schedule: Schedule, is_control_excited: bool, is_target_rotated: bool = False
) -> None:
    """Resets q12 and q13 and prepares q12, the target, in |1> or, rotated, on the
    equator, and q13, the control, in |1> if it is excited"""
    schedule.add(Reset("q12", "q13"))
    target = schedule.add(X90("q12") if is_target_rotated else X("q12"))
    if is_control_excited:
        schedule.add(X("q13"), ref_op=target, ref_pt="start")


def _add_flux_pulse(schedule: Schedule, duration: float) -> None:
    """Adds a flux pulse of the duration on q12 and q13's coupler"""
    schedule.add(
        SoftSquarePulse(
            amp=_CZ_AMPLITUDE, duration=duration, port="q12_q13:fl", clock="q12_q13.cz"
        )
    )


def _add_pair_measurement(schedule: Schedule, index: int) -> None:
    """Adds single shots of q12 and q13, at once, on channels 0 and 1"""
    first = schedule.add(Measure("q12", acq_index=index, bin_mode=BinMode.APPEND))
    schedule.add(
        Measure("q13", acq_index=index, acq_channel=1, bin_mode=BinMode.APPEND),
        ref_op=first,
        ref_pt="start",
    )


def _get_pair_levels(device, acquired, points: int) -> np.ndarray:
    """Gets the levels of q12 and q13 in each shot of each point, the nearest of each
    one's level's responses"""
    levels = []
    for channel, element in enumerate(("q12", "q13")):
        values = acquired[channel].values.reshape(_PAIR_REPETITIONS, points)
        responses = np.array(
            [_get_response(device, level, element) for level in range(3)]
        )
        distances = np.abs(values[..., np.newaxis] - responses)
        levels.append(np.argmin(distances, axis=-1))
    return np.stack(levels, axis=-1)


def _get_full_swap_duration() -> float:
    """Gets the duration of the flux pulse that takes |11> to |02> and back, on a grid of
    4 ns"""
    coupler = Coupler("q12_q13", _COUPLER, (_QUBIT, _CHIP.qubits["q13"]))
    durations = np.arange(24, 400, 4)
    returned = [
        abs(_get_flux_unitary(coupler, duration)[_STATE_11, _STATE_11]) ** 2
        for duration in durations
    ]
    swapped = int(np.argmin(returned))
    return float(durations[swapped + np.argmax(returned[swapped:])]) * 1e-9


def _get_flux_unitary(coupler: Coupler, duration: int) -> np.ndarray:
    """Gets the coupler's unitary of a resonant flux pulse of the nanoseconds"""
    samples = soft_square(np.arange(duration) * 1e-9, _CZ_AMPLITUDE)
    return coupler.get_unitary(samples, ("test", duration), _get_resonance(), 0.0)


def _get_resonance() -> float:
    """Gets the difference of the energies of q12 and q13's |02> and |11>, parked"""
    coupler = Coupler("q12_q13", _COUPLER, (_QUBIT, _CHIP.qubits["q13"]))
    residual = coupler.residual_energies
    frequencies = _CHIP.qubits["q13"].frequency - _QUBIT.frequency
    return frequencies + residual[_STATE_02] - residual[_STATE_11]
