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
"""The experiments of a gate-level job's circuits, compiled for a quantify device, whether
clusters or the simulator then run them"""

from typing import List

from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

from libs.device_parameters import BackendConfig

from ..base.circuits import CircuitExperiment, QasmCircuit
from ..base.qasm import to_native_circuit
from ..base.quantum_job import get_experiment_name
from ..base.quantum_job.dtos import NativeQobjConfig
from .circuits import DeviceFrames, get_circuit_schedule
from .device import (
    get_edge_names,
    get_element_name,
    get_frame_frequencies,
    get_mirrored_elements,
)
from .scheduling import get_compilation_config


def get_circuit_experiments(
    device: QuantumDevice,
    backend_config: BackendConfig,
    compiler: SerialCompiler,
    circuits: List[QasmCircuit],
    native_config: NativeQobjConfig,
) -> List[CircuitExperiment[Schedule]]:
    """Compiles the circuits of a gate-level job to gate-level schedules for the device's
    transmons, each circuit qubit i being the i-th qubit by its number

    Args:
        device: the quantum device of the calibration
        backend_config: the backend config
        compiler: the compiler of the device's schedules
        circuits: the circuits of the job
        native_config: the native config for the job

    Returns:
        an experiment per circuit, whose duration its compilation gives

    Raises:
        UnsupportedCircuitError: a circuit has a gate or qubits the device lacks
    """
    dt = backend_config.general_config.dt
    qubit_ids = backend_config.device_config.qubit_numbers
    element_names = [get_element_name(qubit_id) for qubit_id in qubit_ids]
    edge_names = get_edge_names(device)
    frames = DeviceFrames(dt=dt, frequencies=get_frame_frequencies(device))
    mirrored_elements = get_mirrored_elements(device, device.hardware_config())
    compilation_config = get_compilation_config(device)

    experiments = []
    for idx, circuit in enumerate(circuits):
        native_circuit = to_native_circuit(circuit, dt=dt)
        schedule = get_circuit_schedule(
            native_circuit,
            native_config,
            element_names=element_names,
            edge_names=edge_names,
            calibrated_elements=list(device.elements()),
            frames=frames,
            mirrored_elements=mirrored_elements,
        )
        compiled = compiler.compile(schedule, config=compilation_config)
        experiments.append(
            CircuitExperiment(
                name=get_experiment_name(circuit.name, idx + 1),
                schedule=schedule,
                duration=compiled.duration,
                readout=native_circuit.to_readout(qubit_ids),
            )
        )
    return experiments
