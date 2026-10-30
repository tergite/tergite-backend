# This code is part of Tergite
#
# (C) Pontus Vikstål, Stefan Hill (2024)
# (C) Martin Ahindura (2025)
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""The simulator of a single transmon qubit"""
from typing import Any

import jax
import numpy as np
from qiskit.providers import QubitProperties
from qiskit.providers.models import GateConfig, PulseBackendConfiguration, PulseDefaults
from qiskit.pulse import Acquire, AcquireChannel, MemorySlot, Schedule
from qiskit.transpiler import Target
from qiskit_dynamics import DynamicsBackend, Solver

from app.libs.device_parameters import BackendConfig
from app.libs.quantum_executor.qiskit.functions import (
    fit_lda,
    get_basis_statevector,
    get_lo_range,
)

# configure jax to use 64 bit mode
jax.config.update("jax_enable_x64", True)
# configure jax to use 64 bit mode
jax.config.update("jax_platform_name", "cpu")


def get_one_qubit_backend(
    backend_config: BackendConfig,
    alpha: float = -0.17e9,
    r: float = 1e9,
    atol: float = 1e-6,
    rtol: float = 1e-6,
    dim: int = 4,
    is_noisy: bool = True,
    **options: Any,
) -> DynamicsBackend:
    r"""Gets the backend for pulse simulations on the first qubit of the configuration

    Args:
        backend_config: the configuration of this backend
        alpha: The qubit anharmonicity in Hz
        r: The rabi rate in Hz
        atol: The absolute tolerance for the numerical solver
        rtol: The relative tolerance for the numerical solver
        dim: The dimension of the Hilbert space for the system
        is_noisy: whether the simulation has noise
        **options: Additional keyword arguments passed to the DynamicsBackend
    """
    dt = backend_config.general_config.dt
    qubit_conf = backend_config.calibration_config.qubit[0]
    freq = qubit_conf["frequency"]
    t1 = qubit_conf["t1_decoherence"]
    t2 = qubit_conf["t2_decoherence"]

    return DynamicsBackend(
        solver=_get_solver(
            dt, freq=freq, t1=t1, t2=t2, alpha=alpha, r=r, dim=dim, is_noisy=is_noisy
        ),
        target=Target(
            num_qubits=1,
            qubit_properties=[QubitProperties(frequency=freq, t1=t1, t2=t2)],
            dt=dt,
            granularity=1,
        ),
        solver_options={
            "method": "jax_odeint",
            "atol": atol,
            "rtol": rtol,
            "hmax": dt,
        },
        configuration=_get_configuration(
            backend_config, freq=freq, alpha=alpha, dim=dim
        ),
        defaults=PulseDefaults(
            qubit_freq_est=[freq / 1e9],
            meas_freq_est=[0],
            buffer=0,
            pulse_library=[],
            cmd_def=[],
            meas_kernel=None,
            discriminator=None,
        ),
        subsystem_dims=[dim],
        **options,
    )


def train_one_qubit_discriminator(
    backend: DynamicsBackend, backend_config: BackendConfig, shots: int = 1024
) -> dict:
    """Generates |0> and |1> states, and trains a linear discriminator on them

    Args:
        backend: the backend to simulate the states on
        backend_config: the configuration of the backend
        shots: number of shots for generating i q data

    Returns:
        Discriminator object as json in the format to store it in the database
    """
    schedule = Schedule((0, Acquire(1, AcquireChannel(0), MemorySlot(0))))
    dims = backend.options.subsystem_dims
    iq_values = [
        backend.run(
            [schedule],
            shots=shots,
            initial_state=get_basis_statevector(dims, mode=0, level=level),
        )
        .result()
        .data()["memory"]
        .reshape(shots, 2)
        for level in (0, 1)
    ]
    lda_result = fit_lda(*iq_values)

    # Bring it to the right format
    return {
        "lda": {
            qubit_id: lda_result for qubit_id in backend_config.device_config.qubit_ids
        }
    }


def _get_configuration(
    backend_config: BackendConfig, freq: float, alpha: float, dim: int
) -> PulseBackendConfiguration:
    dt = backend_config.general_config.dt
    gates_configs = [GateConfig(name=k, **v) for k, v in backend_config.gates.items()]

    return PulseBackendConfiguration(
        backend_name=backend_config.general_config.name,
        backend_version=backend_config.general_config.version,
        n_qubits=backend_config.general_config.num_qubits,
        basis_gates=list(backend_config.gates.keys()),
        gates=gates_configs[0],
        local=True,
        simulator=backend_config.general_config.simulator,
        conditional=False,
        open_pulse=backend_config.general_config.open_pulse,
        memory=True,
        max_shots=4000,
        coupling_map=backend_config.device_config.coupling_map,
        meas_map=backend_config.device_config.meas_map,
        n_uchannels=0,
        u_channel_lo=[],
        meas_levels=[1, 2],
        qubit_lo_range=[get_lo_range(freq)],
        meas_lo_range=[],
        dt=dt / 1e-9,  # in nanoseconds
        dtm=dt / 1e-9,  # in nanoseconds
        rep_times=[],
        meas_kernels=["boxcar"],
        discriminators=["max_1Q_fidelity"],
        description=backend_config.general_config.description,
        hamiltonian={
            "h_str": [
                "2*np.pi*f*N0",
                "np.pi*alpha*N0*N0",
                "-np.pi*alpha*N0",
                "X0||D0",
            ],
            "vars": {"f": freq, "alpha": alpha},
            "qub": {"0": dim},
            "osc": {},
            "description": "A single transmon Hamiltonian with 4 levels",
        },
    )


def _get_solver(
    dt: float,
    freq: float,
    t1: float,
    t2: float,
    alpha: float,
    r: float,
    dim: int,
    is_noisy: bool,
) -> Solver:
    a = np.diag(np.sqrt(np.arange(1, dim)), 1)  # annihilation operator
    adag = np.diag(np.sqrt(np.arange(1, dim)), -1)  # creation operator
    N = np.diag(np.arange(dim))  # number operator
    ident = np.eye(dim, dtype=complex)

    # Create static Hamiltonian
    static_ham = 2 * np.pi * freq * N + np.pi * alpha * N * (N - ident)

    # Create drive operator
    drive_op = 2 * np.pi * r * (a + adag)

    # Setup static dissipators
    static_dissipators = None
    if is_noisy:
        static_dissipators = []
        t2_eff = 1.0 / (1.0 / t2 - 1.0 / 2.0 / t1)
        static_dissipators.append(1 / np.sqrt(t1) * a)
        static_dissipators.append(1 / np.sqrt(2 * t2_eff) * 2 * N)

    return Solver(
        static_hamiltonian=static_ham,
        hamiltonian_operators=[drive_op],
        rotating_frame=static_ham,
        hamiltonian_channels=["d0"],
        channel_carrier_freqs={"d0": freq},
        static_dissipators=static_dissipators,
        dt=dt,
        array_library="numpy",
    )
