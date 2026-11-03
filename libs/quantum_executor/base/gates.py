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
in the circuit and each angle in radians, and the pulse operations of its pulse-level
blocks, on the frames the device publishes"""
import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple, Union

from .circuits import CircuitReadout, Measurement

# a frame's name, as the hardware config names its port: q12_drive, q12_readout, q12_q13_coupler
FRAME_NAME_REGEX = re.compile(r"^q(\d+)_(drive|readout)$|^q(\d+)_q(\d+)_(coupler)$")


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


@dataclass(frozen=True)
class Frame:
    """A frame the device publishes: a qubit's drive or readout, or a coupler's flux, its
    qubits by the numbers in their ids, played at a calibrated frequency"""

    kind: str
    qubit_ids: Tuple[int, ...]

    @classmethod
    def from_name(cls, name: str) -> "Frame":
        """Gets the frame of the name, e.g. q12_drive, q12_readout or q12_q13_coupler

        Args:
            name: the frame's name

        Returns:
            the frame

        Raises:
            ValueError: {name} is not a frame's name
        """
        matched = FRAME_NAME_REGEX.match(name)
        if matched is None:
            raise ValueError(f"{name} is not a frame's name")
        qubit, kind, parent, child, coupler = matched.groups()
        if coupler:
            return cls(kind=coupler, qubit_ids=(int(parent), int(child)))
        return cls(kind=kind, qubit_ids=(int(qubit),))

    @property
    def name(self) -> str:
        """The frame's name, its qubits' ids as the hardware config writes them"""
        return "_".join(
            [*(f"q{qubit_id:02d}" for qubit_id in self.qubit_ids), self.kind]
        )


@dataclass(frozen=True)
class Play:
    """A waveform played on the frame, one complex sample per sample time, each of a
    magnitude of 1 at most"""

    frame: Frame
    samples: Tuple[complex, ...]


@dataclass(frozen=True)
class ShiftPhase:
    """A shift of the frame's phase, in radians"""

    frame: Frame
    phase: float


@dataclass(frozen=True)
class SetPhase:
    """A setting of the frame's phase, in radians"""

    frame: Frame
    phase: float


@dataclass(frozen=True)
class ShiftFrequency:
    """A shift of the frame's frequency, in hertz"""

    frame: Frame
    frequency: float


@dataclass(frozen=True)
class SetFrequency:
    """A setting of the frame's frequency, in hertz"""

    frame: Frame
    frequency: float


@dataclass(frozen=True)
class FrameDelay:
    """A wait of the frame, in seconds"""

    frame: Frame
    duration: float


@dataclass(frozen=True)
class FrameBarrier:
    """A point the frames' operations do not cross; every frame of the block if none"""

    frames: Tuple[Frame, ...]


@dataclass(frozen=True)
class Capture:
    """An acquisition on a readout frame, for the duration in seconds, into the classical
    bit, on the channel, as a measurement of the frame's qubit"""

    frame: Frame
    duration: float
    clbit: int
    channel: int


PulseOperation = Union[
    Play,
    ShiftPhase,
    SetPhase,
    ShiftFrequency,
    SetFrequency,
    FrameDelay,
    FrameBarrier,
    Capture,
]


@dataclass(frozen=True)
class PulseBlock:
    """Pulse operations on the device's frames, of a cal block or of a gate's defcal: each
    frame runs its own operations one after the other, from the block's start, and what
    follows the block starts once all its frames are done"""

    operations: Tuple[PulseOperation, ...]


Gate = Union[Rxy, Rz, CZ, Measure, Reset, Delay, Barrier, PulseBlock]


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
            the readout of the circuit, its measurements and captures by channel
        """
        acquisitions: List[Tuple[int, Measurement]] = []
        for gate in self.gates:
            if isinstance(gate, Measure):
                measurement = Measurement(
                    qubit_id=qubit_ids[gate.qubit], clbit=gate.clbit
                )
                acquisitions.append((gate.channel, measurement))
            elif isinstance(gate, PulseBlock):
                acquisitions.extend(
                    (op.channel, Measurement(op.frame.qubit_ids[0], op.clbit))
                    for op in gate.operations
                    if isinstance(op, Capture)
                )
        return CircuitReadout(
            name=self.name,
            num_clbits=self.num_clbits,
            measurements=tuple(m for _, m in sorted(acquisitions, key=lambda a: a[0])),
        )
