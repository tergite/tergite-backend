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
"""Tests for the pure steps of building the qiskit simulators"""
import numpy as np
import pytest

pytest.importorskip("qiskit_dynamics")

from libs.quantum_executor.qiskit.backends.two_qubit import (
    _get_drive_operators,
    _get_ladder_operators,
    _get_static_hamiltonian,
    _TransmonParams,
)
from libs.quantum_executor.qiskit.functions import fit_lda, get_basis_statevector

_DIM = 3
_PARAMS = _TransmonParams(
    f0=4.8e9,
    alpha0=-0.17e9,
    f1=4.4e9,
    alpha1=-0.16e9,
    f2=7.0e9,
    alpha2=-0.1e9,
    g02=0.07e9,
    g12=0.07e9,
    coupler={
        "cz_pulse_amplitude": 0.1,
        "cz_pulse_dc_bias": 0.1,
        "frequency_detuning": 0.0,
        "cz_pulse_phase_offset": 0.0,
        "cz_pulse_duration_before": 0.0,
        "cz_pulse_duration_rise": 1e-8,
        "cz_pulse_duration_constant": 1e-7,
    },
)


def test_each_mode_has_its_own_ladder_operators():
    operators = _get_ladder_operators(_DIM)
    a0, a1, a2 = operators.annihilators

    numbers_from_ladders = [
        creator @ annihilator
        for creator, annihilator in zip(operators.creators, operators.annihilators)
    ]
    assert all(
        np.allclose(got, expected)
        for got, expected in zip(numbers_from_ladders, operators.numbers)
    )
    assert np.allclose(a0 @ a1, a1 @ a0) and np.allclose(a1 @ a2, a2 @ a1)
    assert a0.shape == (_DIM**3, _DIM**3)


def test_static_hamiltonian_gives_a_qubit_its_frequency():
    hamiltonian = _get_static_hamiltonian(_PARAMS, _get_ladder_operators(_DIM))

    # qubit 0 is the least significant mode, so |n0=1> is the state of index 1
    assert np.allclose(hamiltonian, hamiltonian.conj().T)
    assert hamiltonian[0, 0] == 0
    assert np.isclose(hamiltonian[1, 1], 2 * np.pi * _PARAMS.f0)
    assert np.isclose(hamiltonian[_DIM, _DIM], 2 * np.pi * _PARAMS.f1)


def test_drive_operators_drive_the_qubits_and_number_the_coupler():
    operators = _get_ladder_operators(_DIM)

    drive_0, drive_1, coupler_drive = _get_drive_operators(operators, r=1e9)

    assert np.allclose(drive_0, drive_0.conj().T)
    assert np.allclose(
        drive_1, 2 * np.pi * 1e9 * (operators.annihilators[1] + operators.creators[1])
    )
    assert np.allclose(coupler_drive, operators.numbers[2])


def test_lda_tells_the_excited_points_from_the_ground_ones():
    rng = np.random.default_rng(0)
    iq_0 = rng.normal(loc=(0.0, 0.0), scale=0.1, size=(50, 2))
    iq_1 = rng.normal(loc=(1.0, 1.0), scale=0.1, size=(50, 2))

    lda = fit_lda(iq_0, iq_1)

    def score(point):
        return lda["intercept"] + lda["coef_0"] * point[0] + lda["coef_1"] * point[1]

    assert set(lda) == {"intercept", "coef_0", "coef_1"}
    assert score((0.0, 0.0)) < 0 < score((1.0, 1.0))


@pytest.mark.parametrize(
    "dims, mode, level, expected_index",
    [([4], 0, 1, 1), ([3, 3, 3], 0, 1, 1), ([3, 3, 3], 1, 1, 3), ([3, 3, 3], 2, 2, 18)],
)
def test_basis_state_excites_only_its_mode(dims, mode, level, expected_index):
    state = get_basis_statevector(dims, mode=mode, level=level)

    assert np.flatnonzero(state.data).tolist() == [expected_index]
    assert len(state.data) == int(np.prod(dims))


def test_results_of_a_partial_measurement_are_its_qubits_alone():
    """qiskit-dynamics returns a column for every memory slot up to the last acquired, so a
    measurement of qubit 1 alone keeps no column for the unmeasured qubit 0"""
    from libs.quantum_executor.qiskit.executor import _QiskitDynMeasReturn, _to_xarray

    shots = np.array([[[0.0, 0.0], [1.0, 2.0]], [[0.0, 0.0], [3.0, 4.0]]])

    dataset = _to_xarray(data=shots, meas_return=_QiskitDynMeasReturn.SINGLE, slots=[1])

    assert list(dataset.data_vars) == ["1"]
    np.testing.assert_array_equal(dataset["1"].data[:, 0], [1 + 2j, 3 + 4j])
