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
"""The joint state of transmons that couplers' pulses may entangle"""

from typing import List, Sequence

import numpy as np

from .transmon import LEVELS


class Register:
    """The density matrix of transmons, its axes each transmon's ket, then each one's bra,
    in the order of their names"""

    def __init__(self, elements: Sequence[str]) -> None:
        """
        Args:
            elements: the transmons' names, each starting in |0>
        """
        self.elements = list(elements)
        self._state = np.zeros((LEVELS,) * (2 * len(elements)), complex)
        self._state[(0,) * (2 * len(elements))] = 1.0

    def apply_local(self, element: str, superoperator: np.ndarray) -> None:
        """Applies a superoperator to one transmon

        Args:
            element: the transmon's name
            superoperator: the superoperator on its density matrix, vectorized by rows
        """
        ket, bra = self._get_axes(element)
        operator = superoperator.reshape((LEVELS,) * 4)
        state = np.tensordot(operator, self._state, axes=([2, 3], [ket, bra]))
        self._state = np.moveaxis(state, [0, 1], [ket, bra])

    def apply_unitary(self, elements: Sequence[str], unitary: np.ndarray) -> None:
        """Applies a unitary to transmons

        Args:
            elements: the transmons' names, the first the most significant in the
                unitary's order of states
            unitary: the unitary on their joint states
        """
        count = len(elements)
        operator = unitary.reshape((LEVELS,) * (2 * count))
        kets = [self._get_axes(element)[0] for element in elements]
        bras = [self._get_axes(element)[1] for element in elements]
        state = np.tensordot(
            operator, self._state, axes=(range(count, 2 * count), kets)
        )
        state = np.moveaxis(state, range(count), kets)
        adjoint = operator.conj()
        state = np.tensordot(adjoint, state, axes=(range(count, 2 * count), bras))
        self._state = np.moveaxis(state, range(count), bras)

    def reset(self, element: str) -> None:
        """Takes a transmon to |0>, leaving the others' state as it is

        Args:
            element: the transmon's name
        """
        ket, bra = self._get_axes(element)
        traced = np.trace(self._state, axis1=ket, axis2=bra)
        ground = np.zeros((LEVELS, LEVELS))
        ground[0, 0] = 1.0
        state = np.multiply.outer(ground, traced)
        self._state = np.moveaxis(state, [0, 1], [ket, bra])

    def get_probabilities(self, elements: Sequence[str]) -> np.ndarray:
        """Gets the joint probabilities of the transmons' levels

        Args:
            elements: the transmons' names

        Returns:
            the probabilities, an axis per transmon in their order, non-negative and
            summing to 1
        """
        count = len(self.elements)
        diagonal = np.einsum(_get_diagonal_subscripts(count), self._state).real
        others = tuple(
            index
            for index, element in enumerate(self.elements)
            if element not in elements
        )
        probabilities = diagonal.sum(axis=others) if others else diagonal
        kept = [element for element in self.elements if element in elements]
        probabilities = np.transpose(
            probabilities, [kept.index(element) for element in elements]
        )
        probabilities = np.clip(probabilities, 0, None)
        return probabilities / probabilities.sum()

    def _get_axes(self, element: str) -> List[int]:
        """Gets the transmon's ket and bra axes"""
        position = self.elements.index(element)
        return [position, position + len(self.elements)]


def _get_diagonal_subscripts(count: int) -> str:
    """Gets einsum's subscripts of the diagonal of a density matrix of the transmons"""
    letters = "abcdefghij"[:count]
    return f"{letters}{letters}->{letters}"
