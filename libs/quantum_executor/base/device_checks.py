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
"""Whether a circuit of native gates fits the device its config describes: its qubits and
couplers"""

from typing import Iterable, Sequence, Tuple

from utils.exc import UnsupportedCircuitError

from . import gates


def check_circuit_fits_device(
    circuit: gates.NativeCircuit,
    qubit_ids: Sequence[int],
    couplings: Iterable[Tuple[int, int]],
) -> None:
    """Refuses a circuit that needs more qubits than the device has, or a coupler it
    lacks, as an executor compiling it would; circuit qubit i is the device's i-th qubit

    What the device's calibration holds, as which qubits are calibrated, is not checked:
    it may change before the circuit runs.

    Args:
        circuit: the circuit of native gates
        qubit_ids: the numbers in the device's qubits' ids, in order
        couplings: the pairs of qubits the device's couplers couple, by the numbers in
            their ids, in either order

    Raises:
        UnsupportedCircuitError: circuit {name} has {n} qubits, but the device has {m}
        UnsupportedCircuitError: qubits {a} and {b} have no coupler
    """
    if circuit.num_qubits > len(qubit_ids):
        raise UnsupportedCircuitError(
            f"circuit {circuit.name} has {circuit.num_qubits} qubits, "
            f"but the device has {len(qubit_ids)}"
        )

    coupled = {frozenset(pair) for pair in couplings}
    for gate in circuit.gates:
        if not isinstance(gate, gates.CZ):
            continue
        control, target = qubit_ids[gate.control], qubit_ids[gate.target]
        if frozenset((control, target)) not in coupled:
            raise UnsupportedCircuitError(
                f"qubits {_get_name(control)} and {_get_name(target)} have no coupler"
            )


def _get_name(qubit_id: int) -> str:
    """The qubit's name, as its transmon's, e.g. q06"""
    return f"q{qubit_id:02d}"
