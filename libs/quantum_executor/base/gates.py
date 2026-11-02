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
"""The native gates every executor compiles a circuit's gates to, each qubit by its index
in the circuit and each angle in radians"""
from dataclasses import dataclass
from typing import Sequence, Tuple, Union

from .circuits import CircuitReadout, Measurement


@dataclass(frozen=True)
class Rxy:
    """A rotation by theta about the axis at phi from x in the xy-plane"""

    qubit: int
    theta: float
    phi: float


@dataclass(frozen=True)
class Rz:
    """A rotation by theta about z, a change of the qubit's frame"""

    qubit: int
    theta: float


@dataclass(frozen=True)
class CZ:
    """A controlled-Z between two coupled qubits"""

    control: int
    target: int


@dataclass(frozen=True)
class Measure:
    """A measurement of the qubit into the classical bit, acquired on the channel"""

    qubit: int
    clbit: int
    channel: int


@dataclass(frozen=True)
class Reset:
    """A reset of the qubit to its ground state"""

    qubit: int


@dataclass(frozen=True)
class Delay:
    """A wait of the qubits, in seconds"""

    qubits: Tuple[int, ...]
    duration: float


@dataclass(frozen=True)
class Barrier:
    """A point the qubits' operations do not cross"""

    qubits: Tuple[int, ...]


Gate = Union[Rxy, Rz, CZ, Measure, Reset, Delay, Barrier]


@dataclass(frozen=True)
class NativeCircuit:
    """A circuit of native gates, in the order they apply

    Attributes:
        name: the circuit's name
        num_qubits: the number of the circuit's qubits
        num_clbits: the number of the circuit's classical bits
        gates: the circuit's gates; the i-th Measure is acquired on channel i
    """

    name: str
    num_qubits: int
    num_clbits: int
    gates: Tuple[Gate, ...]

    def to_readout(self, qubit_ids: Sequence[int]) -> CircuitReadout:
        """Gets where this circuit's acquisitions go on the device

        Args:
            qubit_ids: the number in each device qubit's id, by the qubit's index in the
                circuit, e.g. [12, 13] when q12 is circuit qubit 0

        Returns:
            the readout of the circuit, its measurements by channel
        """
        measures = sorted(
            (gate for gate in self.gates if isinstance(gate, Measure)),
            key=lambda gate: gate.channel,
        )
        return CircuitReadout(
            name=self.name,
            num_clbits=self.num_clbits,
            measurements=tuple(
                Measurement(qubit_id=qubit_ids[gate.qubit], clbit=gate.clbit)
                for gate in measures
            ),
        )
