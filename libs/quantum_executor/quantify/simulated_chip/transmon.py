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
"""A simulated transmon of three levels, in the frame that rotates at its frequency for
each excitation: the superoperators of its idling and its pulses, kept for reuse"""
import math
from typing import Dict, Hashable, List, Tuple

import numpy as np
import qutip
from scipy.linalg import expm

from libs.device_parameters.dtos import ChipQubit

from .timeline import SAMPLE

LEVELS = 3
# the excitations by which each element of a vectorized density matrix turns in a frame
_COHERENCES = (np.arange(LEVELS)[:, np.newaxis] - np.arange(LEVELS)).ravel()
_ANNIHILATOR = qutip.destroy(LEVELS)
_NUMBER = qutip.num(LEVELS)
_IDENTITY = qutip.qeye(LEVELS)
# how many superoperators a transmon keeps, of idling and of pulses
_MAX_KEPT = 50_000


class Transmon:
    """A transmon of the chip, whose drive's pulses, at their clock's frequency, and
    idling evolve its density matrix"""

    def __init__(self, qubit: ChipQubit) -> None:
        """
        Args:
            qubit: the chip's parameters of the transmon
        """
        self.qubit = qubit
        anharmonicity = 2 * math.pi * qubit.anharmonicity
        self._static = anharmonicity / 2 * _NUMBER * (_NUMBER - _IDENTITY)
        self._collapses = _get_collapses(qubit)
        self._idles: Dict[int, np.ndarray] = {}
        self._pulses: Dict[Hashable, np.ndarray] = {}

    def get_idle(self, samples: int) -> np.ndarray:
        """Gets the superoperator of idling, undriven, for the samples

        Args:
            samples: how many samples it idles

        Returns:
            the superoperator on its density matrix, vectorized by rows
        """
        propagator = self._idles.get(samples)
        if propagator is None:
            liouvillian = _get_liouvillian(self._static, self._collapses)
            propagator = expm(liouvillian * samples * SAMPLE)
            _keep(self._idles, samples, propagator)
        return propagator

    def get_drive(
        self,
        samples: np.ndarray,
        key: Hashable,
        detuning: float,
        phase: float,
    ) -> np.ndarray:
        """Gets the superoperator of a pulse on its drive

        Args:
            samples: the pulse's envelope, a sample each SAMPLE
            key: the same for pulses of the same samples
            detuning: the pulse's clock's frequency less the transmon's, in hertz
            phase: the turns by which the clock's phase, at the pulse's start, leads the
                transmon's frame

        Returns:
            the superoperator on its density matrix, vectorized by rows
        """
        cache_key = (key, round(detuning))
        propagator = self._pulses.get(cache_key)
        if propagator is None:
            propagator = self._get_pulse_propagator(samples, detuning)
            _keep(self._pulses, cache_key, propagator)
        turns = detuning * len(samples) * SAMPLE + phase
        before = np.exp(2j * math.pi * phase * _COHERENCES)
        after = np.exp(-2j * math.pi * turns * _COHERENCES)
        return after[:, np.newaxis] * propagator * before

    def _get_pulse_propagator(self, samples: np.ndarray, detuning: float) -> np.ndarray:
        """Gets the superoperator of the pulse, in the frame of its clock at phase 0, each
        run of equal samples exponentiated at once"""
        static = self._static - 2 * math.pi * detuning * _NUMBER
        rate = math.pi * self.qubit.rabi_frequency
        propagator = np.eye(LEVELS * LEVELS, dtype=complex)
        for value, length in _get_runs(samples):
            # the mixers send the envelope's upper sideband, so the transmon's raising
            # takes its conjugate
            drive = rate * (np.conj(value) * _ANNIHILATOR.dag() + value * _ANNIHILATOR)
            liouvillian = _get_liouvillian(static + drive, self._collapses)
            propagator = expm(liouvillian * length * SAMPLE) @ propagator
        return propagator


def _get_collapses(qubit: ChipQubit) -> List[qutip.Qobj]:
    """Gets the collapse operators of the transmon's relaxation at its T1 and of its
    pure dephasing, which with it makes its T2"""
    dephasing = 1 / qubit.t2 - 1 / (2 * qubit.t1)
    collapses = [math.sqrt(1 / qubit.t1) * _ANNIHILATOR]
    if dephasing > 0:
        collapses.append(math.sqrt(2 * dephasing) * _NUMBER)
    return collapses


def _get_liouvillian(
    hamiltonian: qutip.Qobj, collapses: List[qutip.Qobj]
) -> np.ndarray:
    """Gets the Liouvillian of the Hamiltonian and collapse operators, on density
    matrices vectorized by rows, not QuTiP's columns"""
    liouvillian = qutip.liouvillian(hamiltonian, collapses).full()
    by_columns = liouvillian.reshape((LEVELS,) * 4)
    return by_columns.transpose(1, 0, 3, 2).reshape((LEVELS * LEVELS,) * 2)


def _get_runs(samples: np.ndarray) -> Tuple[Tuple[complex, int], ...]:
    """Gets the runs of equal samples, each its value and length"""
    changes = np.flatnonzero(samples[1:] != samples[:-1]) + 1
    starts = np.concatenate(([0], changes))
    lengths = np.diff(np.concatenate((starts, [len(samples)])))
    return tuple(zip(samples[starts], lengths))


def _keep(kept: Dict, key: Hashable, propagator: np.ndarray) -> None:
    """Keeps the propagator by its key, forgetting all once there are too many"""
    if len(kept) >= _MAX_KEPT:
        kept.clear()
    kept[key] = propagator
