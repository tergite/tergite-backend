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
"""Writes seeded random circuits, each as the OpenQASM 3 the SDK sends today and as the
PulseQobj the SDK of v2026.06.0 sent for it; run with that SDK installed

Usage: python sdk_circuits.py <device_info.json> <circuits.json> <seed> <count>
"""

import json
import math
import random
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from qiskit.circuit import QuantumCircuit
from tergite.compat.qiskit.qobj.encoder import IQXJsonEncoder
from tergite.services.api_client.dtos import DeviceCalibration, TergiteBackendConfig
from tergite.types.backend import OpenPulseBackend

NUM_QUBITS = 3
# the circuit qubits of the calibrated coupler, q12 and q13, in either order
COUPLED_QUBITS = [(0, 1), (1, 0)]
SHOTS = 1024
_GATE_WEIGHTS = {
    "rx": 30,
    "rz": 25,
    "cz": 15,
    "delay": 10,
    "barrier": 5,
    "reset": 3,
    "measure": 4,
}


def main() -> None:
    """Writes the circuits of the arguments"""
    info_file, out_file, seed, count = sys.argv[1:5]
    with open(info_file) as file:
        backend = _get_backend(json.load(file))
    rng = random.Random(int(seed))
    circuits = []
    for idx in range(int(count)):
        name = f"random_{idx:03d}"
        circuit, statements = _get_random_circuit(rng, name)
        qobj = backend.make_qobj(circuit, meas_level=2, meas_return="single")
        circuits.append(
            {
                "name": name,
                "qasm": _get_qasm(statements, circuit.num_clbits),
                "num_clbits": circuit.num_clbits,
                "shots": SHOTS,
                "qobj": json.loads(json.dumps(qobj.to_dict(), cls=IQXJsonEncoder)),
            }
        )
    with open(out_file, "w") as file:
        json.dump(circuits, file)


def _get_backend(info: Dict[str, Any]) -> OpenPulseBackend:
    """Gets the SDK's backend of the device and calibration the backend sent MSS, which
    it then had from MSS"""
    OpenPulseBackend._refresh_device_properties = lambda self: None
    backend = OpenPulseBackend(
        data=TergiteBackendConfig(**info["device"]),
        provider=SimpleNamespace(),
        base_url="",
    )
    backend._device_properties = DeviceCalibration(
        **{"last_calibrated": "2026-01-01T00:00:00"} | info["calibration"]
    )
    backend.set_options(shots=SHOTS)
    return backend


def _get_random_circuit(
    rng: random.Random, name: str
) -> Tuple[QuantumCircuit, List[str]]:
    """Gets a random circuit of the gates the SDK's target had, and its OpenQASM 3
    statements, measuring some of its qubits at the end"""
    circuit = QuantumCircuit(NUM_QUBITS, NUM_QUBITS, name=name)
    statements = []
    for _ in range(rng.randint(2, 18)):
        (kind,) = rng.choices(list(_GATE_WEIGHTS), weights=list(_GATE_WEIGHTS.values()))
        qubit = rng.randrange(NUM_QUBITS)
        if kind == "rx":
            theta = rng.choice(
                [math.pi, math.pi / 2, -math.pi / 2, rng.uniform(-math.pi, math.pi)]
            )
            circuit.rx(theta, qubit)
            statements.append(f"rx({theta!r}) ${qubit};")
        elif kind == "rz":
            theta = rng.choice([math.pi / 2, rng.uniform(-math.pi, math.pi)])
            circuit.rz(theta, qubit)
            statements.append(f"rz({theta!r}) ${qubit};")
        elif kind == "cz":
            control, target = rng.choice(COUPLED_QUBITS)
            circuit.cz(control, target)
            statements.append(f"cz ${control}, ${target};")
        elif kind == "delay":
            duration = rng.randint(1, 400)
            circuit.delay(duration, qubit, unit="dt")
            statements.append(f"delay[{duration}dt] ${qubit};")
        elif kind == "barrier":
            qubits = sorted(rng.sample(range(NUM_QUBITS), rng.randint(2, NUM_QUBITS)))
            circuit.barrier(qubits)
            statements.append(f"barrier {', '.join(f'${q}' for q in qubits)};")
        elif kind == "reset":
            circuit.reset(qubit)
            statements.append(f"reset ${qubit};")
        else:
            circuit.measure(qubit, qubit)
            statements.append(f"c[{qubit}] = measure ${qubit};")
    for qubit in sorted(rng.sample(range(NUM_QUBITS), rng.randint(1, NUM_QUBITS))):
        circuit.measure(qubit, qubit)
        statements.append(f"c[{qubit}] = measure ${qubit};")
    return circuit, statements


def _get_qasm(statements: List[str], num_clbits: int) -> str:
    """Gets the OpenQASM 3 program of the statements, on physical qubits"""
    header = f'OPENQASM 3.0;\ninclude "stdgates.inc";\nbit[{num_clbits}] c;\n'
    return header + "\n".join(statements) + "\n"


if __name__ == "__main__":
    main()
