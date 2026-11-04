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
"""A simulated chip's coupler: a flux-tunable transmon between two transmons, whose flux
pulse, modulated at its clock taken down from the CZ mixer's frequency, drives their
parametric CZ

The two transmons and the coupler, three levels each, are diagonalized over the coupler's
flux. The transmons' pair keeps to the states with the coupler in its ground state, which
the flux carries adiabatically, the pulse's modulation driving transitions among them:
|11> to |20> or |02> where it is resonant, and moving their energies, which gives the CZ
its conditional phase and each transmon a phase of its own.
"""
import math
from typing import Dict, Hashable, List, Tuple

import numpy as np
import qutip

from libs.device_parameters.dtos import ChipCoupler, ChipQubit

from .timeline import SAMPLE
from .transmon import LEVELS

# the frequency of the mixer that takes a coupler's clock down to its flux's modulation
DOWNCONVERSION_FREQUENCY = 4.4e9
# the states of the transmons' pair, each its levels, the first transmon's the more
# significant
PAIR_STATES = [(first, second) for first in range(LEVELS) for second in range(LEVELS)]
# the pair's states of each number of excitations, among which the flux moves them
_BLOCKS = [
    [index for index, state in enumerate(PAIR_STATES) if sum(state) == excitations]
    for excitations in range(2 * LEVELS - 1)
]
# the steps of each sample of a pulse, short to follow the pair's GHz differences
_STEPS = 32
# the fluxes the coupler's spectrum is found at, over the reach of a pulse of amplitude 1
_FLUXES = 401
# turns of a modulation's phase at a pulse's start below which its pulses are alike
_PHASE_BINS = 64
_FITS = 8
_MAX_KEPT = 5_000


class Coupler:
    """A coupler of the chip and its two transmons, whose flux pulses evolve the
    transmons' pair"""

    def __init__(
        self,
        name: str,
        coupler: ChipCoupler,
        qubits: Tuple[ChipQubit, ...],
        current: float = 0.0,
    ) -> None:
        """
        Args:
            name: the coupler's name, its transmons' joined by an underscore
            coupler: the chip's parameters of the coupler
            qubits: its transmons, in the order its name gives them, whose frequencies
                and anharmonicities are theirs with the coupler at its flux offset
            current: the DC current of the coupler's SPI rack DAC, in amperes, which
                moves its flux from its offset
        """
        self.coupler = coupler
        self.qubits = qubits
        strengths = tuple(coupler.couplings[qubit] for qubit in name.split("_"))
        bare = _fit_bare(coupler, strengths, qubits)
        parked = coupler.flux_offset + coupler.flux_per_current * current
        reach = 1.05 * abs(coupler.flux_per_amplitude)
        self._fluxes = parked + np.linspace(-reach, reach, _FLUXES)
        self._energies, self._couplings = _get_spectrum(
            coupler, strengths, bare, self._fluxes
        )
        self._flux = parked
        self._parked = _get_spectrum(coupler, strengths, bare, np.array([parked]))[0][0]
        self._unitaries: Dict[Hashable, np.ndarray] = {}

    @property
    def frequencies(self) -> Tuple[float, float]:
        """Its transmons' frequencies, in hertz, at its parked flux: the chip's, but for
        a DC current that moves the flux"""
        first = self._parked[PAIR_STATES.index((1, 0))] - self._parked[0]
        second = self._parked[PAIR_STATES.index((0, 1))] - self._parked[0]
        return first, second

    @property
    def static_energies(self) -> np.ndarray:
        """The parked pair's energy of each state beyond its transmons' own, at their
        frequencies and the chip's anharmonicities, in hertz: the static ZZ of their
        coupling"""
        frequencies = self.frequencies
        own = [
            sum(
                level * frequency + qubit.anharmonicity * level * (level - 1) / 2
                for level, frequency, qubit in zip(state, frequencies, self.qubits)
            )
            for state in PAIR_STATES
        ]
        return self._parked - self._parked[0] - np.array(own)

    def get_unitary(
        self, samples: np.ndarray, key: Hashable, modulation: float, phase: float
    ) -> np.ndarray:
        """Gets the pair's unitary of a flux pulse, in the frame of the parked pair's
        energies

        Args:
            samples: the pulse's envelope, a sample each SAMPLE
            key: the same for pulses of the same samples
            modulation: the frequency of the flux's modulation, in hertz
            phase: the modulation's phase at the pulse's start, in turns

        Returns:
            the unitary on the pair's states, in the order of PAIR_STATES
        """
        phase = round(phase * _PHASE_BINS) % _PHASE_BINS / _PHASE_BINS
        cache_key = (key, round(modulation), phase)
        unitary = self._unitaries.get(cache_key)
        if unitary is None:
            unitary = self._get_unitary(samples, modulation, phase)
            if len(self._unitaries) >= _MAX_KEPT:
                self._unitaries.clear()
            self._unitaries[cache_key] = unitary
        return unitary

    def _get_unitary(
        self, samples: np.ndarray, modulation: float, phase: float
    ) -> np.ndarray:
        """Gets the pulse's unitary, each of its samples held over _STEPS steps"""
        step = SAMPLE / _STEPS
        times = (np.arange(len(samples) * _STEPS) + 0.5) * step
        envelope = np.repeat(np.asarray(samples, complex), _STEPS)
        carrier = np.exp(-2j * math.pi * (phase + modulation * times))
        scale = self.coupler.flux_per_amplitude
        fluxes = self._flux + scale * np.real(envelope * carrier)
        rates = scale * np.real(-2j * math.pi * modulation * envelope * carrier)
        energies = np.stack(
            [np.interp(fluxes, self._fluxes, energy) for energy in self._energies.T],
            axis=1,
        )
        # each state's turns at the middle of each step
        turns = (np.cumsum(energies, axis=0) - energies / 2) * step
        unitary = np.zeros((len(PAIR_STATES),) * 2, complex)
        for block in _BLOCKS:
            unitary[np.ix_(block, block)] = self._get_block(
                block, fluxes, rates, turns, step
            )
        total = energies.sum(axis=0) * step - self._parked * len(samples) * SAMPLE
        return np.exp(-2j * math.pi * total)[:, np.newaxis] * unitary

    def _get_block(
        self,
        block: List[int],
        fluxes: np.ndarray,
        rates: np.ndarray,
        turns: np.ndarray,
        step: float,
    ) -> np.ndarray:
        """Gets the propagator, in the frame of the states' own energies, of a block of
        states the flux moves among"""
        if len(block) == 1:
            return np.ones((1, 1), complex)
        couplings = np.empty((len(fluxes), len(block), len(block)), complex)
        for row, first in enumerate(block):
            for column, second in enumerate(block):
                coupling = np.interp(
                    fluxes, self._fluxes, self._couplings[:, first, second]
                )
                beat = np.exp(2j * math.pi * (turns[:, first] - turns[:, second]))
                couplings[:, row, column] = -rates * coupling * beat
        # the generators are anti-Hermitian: each step's exponential from its eigenbasis
        values, vectors = np.linalg.eigh(1j * couplings)
        phases = np.exp(-1j * values * step)
        steps = vectors * phases[:, np.newaxis, :] @ np.conj(vectors).swapaxes(1, 2)
        return _compute_product(steps)


def _compute_product(steps: np.ndarray) -> np.ndarray:
    """Gets the product of the steps, the last applied last, pairing them up"""
    while len(steps) > 1:
        if len(steps) % 2:
            steps = np.concatenate((steps, np.eye(steps.shape[1])[np.newaxis]))
        steps = steps[1::2] @ steps[0::2]
    return steps[0]


def _fit_bare(
    coupler: ChipCoupler,
    strengths: Tuple[float, ...],
    qubits: Tuple[ChipQubit, ...],
) -> List[Tuple[float, float]]:
    """Gets the bare frequency and anharmonicity of each transmon that with the parked
    coupler dress them to the chip's"""
    bare = [(qubit.frequency, qubit.anharmonicity) for qubit in qubits]
    flux = np.array([coupler.flux_offset])
    for _ in range(_FITS):
        energies = _get_spectrum(coupler, strengths, bare, flux)[0][0]
        for position, qubit in enumerate(qubits):
            one = PAIR_STATES.index((1, 0) if position == 0 else (0, 1))
            two = PAIR_STATES.index((2, 0) if position == 0 else (0, 2))
            frequency = energies[one] - energies[0]
            anharmonicity = energies[two] - energies[0] - 2 * frequency
            bare[position] = (
                bare[position][0] + qubit.frequency - frequency,
                bare[position][1] + qubit.anharmonicity - anharmonicity,
            )
    return bare


def _get_spectrum(
    coupler: ChipCoupler,
    strengths: Tuple[float, ...],
    bare: List[Tuple[float, float]],
    fluxes: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """Gets, at each flux, the energy of each of the pair's states with the coupler in
    its ground state, and the flux's coupling of each two of them: <k|dH/dflux|l> over
    their energies' difference

    Returns:
        the energies, in hertz, by flux and state, and the couplings by flux, state and
        state
    """
    static, coupler_number = _get_hamiltonian(coupler, strengths, bare)
    # each pair's state with the coupler in its ground state, in the triple's space
    bases = [first * LEVELS**2 + second * LEVELS for first, second in PAIR_STATES]

    energies = np.empty((len(fluxes), len(PAIR_STATES)))
    couplings = np.zeros((len(fluxes), len(PAIR_STATES), len(PAIR_STATES)))
    for index, flux in enumerate(fluxes):
        frequency, slope = _get_coupler_frequency(coupler, flux)
        values, vectors = np.linalg.eigh(static + frequency * coupler_number)
        chosen = [int(np.argmax(np.abs(vectors[base]))) for base in bases]
        states = vectors[:, chosen] * np.sign(vectors[bases, chosen])
        energies[index] = values[chosen]
        derivative = slope * states.T @ coupler_number @ states
        differences = energies[index][np.newaxis, :] - energies[index][:, np.newaxis]
        with np.errstate(divide="ignore", invalid="ignore"):
            couplings[index] = np.where(
                np.eye(len(PAIR_STATES), dtype=bool), 0.0, derivative / differences
            )
    return energies, couplings


def _get_hamiltonian(
    coupler: ChipCoupler,
    strengths: Tuple[float, ...],
    bare: List[Tuple[float, float]],
) -> Tuple[np.ndarray, np.ndarray]:
    """Gets the Hamiltonian of the transmons and the coupler, but for the coupler's
    frequency, in hertz, and the coupler's number operator, the transmons first in the
    order the coupler names them"""
    identity = qutip.qeye(LEVELS)
    lowerings = [
        qutip.tensor(
            *[
                qutip.destroy(LEVELS) if axis == position else identity
                for axis in range(3)
            ]
        )
        for position in range(3)
    ]
    numbers = [lowering.dag() * lowering for lowering in lowerings]
    static = coupler.anharmonicity / 2 * numbers[2] * (numbers[2] - 1)
    for position, ((frequency, anharmonicity), strength) in enumerate(
        zip(bare, strengths)
    ):
        number = numbers[position]
        static += frequency * number + anharmonicity / 2 * number * (number - 1)
        exchange = lowerings[position].dag() * lowerings[2]
        static += strength * (exchange + exchange.dag())
    return static.full().real, numbers[2].full().real


def _get_coupler_frequency(coupler: ChipCoupler, flux: float) -> Tuple[float, float]:
    """Gets the coupler's frequency at the flux, in hertz, and its slope, in hertz per
    flux quantum, as a symmetric transmon's"""
    charging = -coupler.anharmonicity
    cosine = math.cos(math.pi * flux)
    root = math.sqrt(abs(cosine))
    frequency = (coupler.max_frequency + charging) * root - charging
    slope = (
        -(coupler.max_frequency + charging)
        * math.pi
        * math.sin(math.pi * flux)
        * math.copysign(1, cosine)
        / (2 * root)
    )
    return frequency, slope
