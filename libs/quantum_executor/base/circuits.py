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
"""A gate-level job: the OpenQASM 3 circuits it uploads, and how each circuit's
acquisitions map to its classical bits"""

from dataclasses import dataclass
from typing import Any, Dict, Generic, List, Literal, Tuple, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class QasmCircuit(BaseModel):
    """A circuit of a gate-level job, written in OpenQASM 3"""

    model_config = ConfigDict(extra="forbid")

    name: str
    qasm: str
    num_clbits: int = Field(ge=0)
    metadata: Dict[str, Any] = {}


class CircuitJobParams(BaseModel):
    """The params of a gate-level job: its circuits and how to measure them

    meas_return applies to meas_level 1 only; meas_level 2 returns every shot.
    """

    model_config = ConfigDict(extra="forbid")

    circuits: List[QasmCircuit] = Field(min_length=1)
    shots: int = Field(gt=0)
    meas_level: Literal[1, 2] = 2
    meas_return: Literal["single", "avg"] = "single"


@dataclass(frozen=True)
class Measurement:
    """A qubit measured into a classical bit

    Attributes:
        qubit_id: the number in the qubit's id, e.g. 12 of q12, which keys its discriminator
        clbit: the index of the classical bit it is measured into
    """

    qubit_id: int
    clbit: int


@dataclass(frozen=True)
class CircuitReadout:
    """Where a circuit's acquisitions go: the i-th measurement is acquired on channel i

    Attributes:
        name: the circuit's name
        num_clbits: the number of the circuit's classical bits
        measurements: the circuit's measurements, in the order they are made
    """

    name: str
    num_clbits: int
    measurements: Tuple[Measurement, ...]

    def to_dict(self) -> dict:
        """Converts this readout into a JSON serializable dict

        Returns:
            the dict of this readout, each measurement as [qubit_id, clbit]
        """
        return {
            "name": self.name,
            "num_clbits": self.num_clbits,
            "measurements": [[m.qubit_id, m.clbit] for m in self.measurements],
        }

    @classmethod
    def from_dict(cls, value: dict) -> "CircuitReadout":
        """Gets the readout of the dict that to_dict made

        Args:
            value: the dict of the readout

        Returns:
            the readout
        """
        return cls(
            name=value["name"],
            num_clbits=value["num_clbits"],
            measurements=tuple(
                Measurement(qubit_id=qubit_id, clbit=clbit)
                for qubit_id, clbit in value["measurements"]
            ),
        )


@dataclass(frozen=True)
class CircuitExperiment(Generic[T]):
    """A circuit compiled to an executor's schedule

    Attributes:
        name: the experiment's name, unique in its job
        schedule: the executor's schedule of the circuit
        duration: the seconds the schedule takes to run
        readout: where the schedule's acquisitions go
    """

    name: str
    schedule: T
    duration: float
    readout: CircuitReadout
