# This code is part of Tergite
#
# (C) Pontus Vikstål, Stefan Hill (2024)
# (C) Chalmers Next Labs (2024)
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
"""The simulator of two transmon qubits and the coupler between them"""
from dataclasses import dataclass
from typing import Any

import numpy as np
from qiskit.providers.models import GateConfig, PulseBackendConfiguration
from qiskit.pulse import Acquire, AcquireChannel, MemorySlot, Schedule
from qiskit.quantum_info import Statevector
from qiskit_dynamics import DynamicsBackend, Solver
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis

from app.libs.device_parameters import BackendConfig
from app.libs.quantum_executor.qiskit.functions import get_lo_range, omega_c

# a qubit's anharmonicity, in Hz, where its calibration has none
_DEFAULT_ANHARMONICITY = -0.17e9


def get_two_qubit_backend(
    backend_config: BackendConfig,
    r: float = 1e9,
    dim: int = 3,
    **options: Any,
) -> DynamicsBackend:
    r"""Gets the backend for pulse simulations on the first two qubits of the configuration
    and their coupler

    Args:
        backend_config: The configuration of this backend.
        r: The Rabi rate in Hz.
        dim: The dimension of the Hilbert space for each qubit.
        **options: Additional keyword arguments passed to the DynamicsBackend.
    """
    if backend_config.general_config.num_qubits < 2:
        raise ValueError("The backend configuration must have at least two qubits.")

    params = _get_transmon_params(backend_config)
    return DynamicsBackend(
        solver=_get_solver(backend_config.general_config.dt, params, r=r, dim=dim),
        solver_options={
            "method": "scipy_expm",
            "max_dt": backend_config.general_config.dt,
        },
        configuration=_get_configuration(backend_config, params, dim=dim),
        subsystem_dims=[dim, dim, dim],
        **options,
    )


def train_two_qubit_discriminator(
    backend: DynamicsBackend, shots: int = 1024, dim: int = 3
) -> dict:
    """Generates |0> and |1> states for each qubit, and trains a linear discriminator on them

    Args:
        backend: the backend to simulate the states on
        shots: number of shots for generating data
        dim: the dimension of the Hilbert space for each qubit, as the backend has it

    Returns:
        Discriminator object as json in the format to store it in the database
    """
    # Generate the iq values for each qubit
    qubit_indexes = [x[0] for x in backend.configuration().meas_map]

    lda_results = {}
    for qubit_idx in qubit_indexes:
        schedule = Schedule((0, Acquire(1, AcquireChannel(qubit_idx), MemorySlot(0))))

        # For |0> state
        y0 = _get_basis_statevector(qubit_idx, level=0, dim=dim)
        job_0 = backend.run([schedule], shots=shots, initial_state=y0)
        i_q_values_0 = job_0.result().data()["memory"].reshape(shots, 2)

        # For |1> state
        y0 = _get_basis_statevector(qubit_idx, level=1, dim=dim)
        job_1 = backend.run([schedule], shots=shots, initial_state=y0)
        i_q_values_1 = job_1.result().data()["memory"].reshape(shots, 2)

        # Combine IQ values and labels
        combined_i_q_values = np.vstack((i_q_values_0, i_q_values_1))
        labels = np.concatenate((np.zeros(shots), np.ones(shots)))

        lda_model = LinearDiscriminantAnalysis()
        lda_model.fit(combined_i_q_values, labels)

        lda_intercepts = lda_model.intercept_.tolist()
        lda_coefs = lda_model.coef_.tolist()

        lda_result = {}
        for i in range(len(lda_intercepts)):
            lda_result["intercept"] = lda_intercepts[i]
            lda_result["coef_0"] = lda_coefs[i][0]
            lda_result["coef_1"] = lda_coefs[i][1]

        lda_results[f"q{qubit_idx}"] = lda_result
    # Return the discriminator configuration
    return {"lda": lda_results}


@dataclass(frozen=True)
class _TransmonParams:
    """The frequencies, anharmonicities and coupling strengths, in Hz, of the two qubits and
    their coupler, whose calibration also shapes its CZ pulse"""

    f0: float
    alpha0: float
    f1: float
    alpha1: float
    f2: float
    alpha2: float
    g02: float
    g12: float
    coupler: dict


def _get_transmon_params(backend_config: BackendConfig) -> _TransmonParams:
    qubit0, qubit1 = backend_config.calibration_config.qubit[:2]
    coupler = backend_config.calibration_config.coupler[0]
    return _TransmonParams(
        f0=qubit0["frequency"],
        alpha0=qubit0.get("anharmonicity", _DEFAULT_ANHARMONICITY),
        f1=qubit1["frequency"],
        alpha1=qubit1.get("anharmonicity", _DEFAULT_ANHARMONICITY),
        f2=coupler["frequency"],
        alpha2=coupler["anharmonicity"],
        g02=coupler["coupling_strength_02"],
        g12=coupler["coupling_strength_12"],
        coupler=coupler,
    )


def _get_configuration(
    backend_config: BackendConfig, params: _TransmonParams, dim: int
) -> PulseBackendConfiguration:
    dt = backend_config.general_config.dt
    gates_configs = [GateConfig(name=k, **v) for k, v in backend_config.gates.items()]

    return PulseBackendConfiguration(
        backend_name=backend_config.general_config.name,
        backend_version=backend_config.general_config.version,
        n_qubits=backend_config.general_config.num_qubits,
        basis_gates=list(backend_config.gates.keys()),
        gates=gates_configs,
        local=True,
        simulator=backend_config.general_config.simulator,
        conditional=False,
        open_pulse=backend_config.general_config.open_pulse,
        memory=True,
        max_shots=4000,
        coupling_map=backend_config.device_config.coupling_map,
        meas_map=backend_config.device_config.meas_map,
        n_uchannels=1,
        u_channel_lo=[get_lo_range(params.f2)],
        meas_levels=[1, 2],
        qubit_lo_range=[
            get_lo_range(params.f0),
            get_lo_range(params.f1),
            get_lo_range(params.f2),
        ],
        meas_lo_range=[],
        dt=dt / 1e-9,  # in nanoseconds
        dtm=dt / 1e-9,  # in nanoseconds
        rep_times=[],
        meas_kernels=["boxcar"],
        discriminators=["lda"],
        description=backend_config.general_config.description,
        hamiltonian={
            "h_str": [
                "2*np.pi*f1*N0",
                "np.pi*alpha1*N0*N0",
                "-np.pi*alpha1*N0",
                "2*np.pi*f2*N1",
                "np.pi*alpha2*N1*N1",
                "-np.pi*alpha2*N1",
                "2*np.pi*J*(a0+a0.dag())*(a1+a1.dag())",
                "X0||D0",
                "X1||D1",
            ],
            "vars": {
                "f1": params.f1,
                "alpha1": params.alpha1,
                "f2": params.f2,
                "alpha2": params.alpha2,
                "J": params.g02,
            },
            "qub": {"0": dim, "1": dim},
            "osc": {},
            "description": "A two-qubit transmon Hamiltonian with 4 levels per qubit",
        },
    )


def _get_solver(dt: float, params: _TransmonParams, r: float, dim: int) -> Solver:
    f0, alpha0, f1, alpha1 = params.f0, params.alpha0, params.f1, params.alpha1
    f2, alpha2, g02, g12 = params.f2, params.alpha2, params.g02, params.g12
    coupler0_conf = params.coupler

    a = np.diag(np.sqrt(np.arange(1, dim)), 1)  # annihilation operator
    adag = np.diag(np.sqrt(np.arange(1, dim)), -1)  # creation operator
    N = np.diag(np.arange(dim))  # number operator

    # Define tensor products for two-qubit system
    I = np.eye(dim)

    # Order of modes: coupler (mode 3), qubit 2 (mode 2), qubit 1 (mode 1)
    a0 = np.kron(I, np.kron(I, a))
    a1 = np.kron(I, np.kron(a, I))
    a2 = np.kron(a, np.kron(I, I))

    adag0 = np.kron(I, np.kron(I, adag))
    adag1 = np.kron(I, np.kron(adag, I))
    adag2 = np.kron(adag, np.kron(I, I))

    N0 = np.kron(I, np.kron(I, N))
    N1 = np.kron(I, np.kron(N, I))
    N2 = np.kron(N, np.kron(I, I))

    ident = np.eye(dim**3, dtype=complex)

    # Create static Hamiltonian
    coupler_f_vs_t = omega_c(
        0,
        delta_0=coupler0_conf["cz_pulse_amplitude"],  # Maximum delta value
        theta=coupler0_conf["cz_pulse_dc_bias"],  # DC bias term
        omega_c0=2 * np.pi * f2,  # Maximum frequency in Hz
        omega_phi=coupler0_conf["frequency_detuning"]
        + 2 * np.pi * (f1 - f0 - alpha0),  # Transition frequency in Hz
        phi=coupler0_conf["cz_pulse_phase_offset"],  # Phase offset
        t_w=coupler0_conf["cz_pulse_duration_before"],  # s, duration before pulse
        t_rf=coupler0_conf["cz_pulse_duration_rise"],  # s, rise time
        t_p=coupler0_conf["cz_pulse_duration_constant"],  # s, constant pulse duration
    )

    static_ham = (
        2 * np.pi * f0 * N0
        + np.pi * alpha0 * N0 @ (N0 - ident)
        + 2 * np.pi * f1 * N1
        + np.pi * alpha1 * N1 @ (N1 - ident)
        + coupler_f_vs_t * N2
        + np.pi * alpha2 * N2 @ (N2 - ident)
        + 2 * np.pi * g02 * ((a0 + adag0) @ (a2 + adag2))
        + 2 * np.pi * g12 * ((a1 + adag1) @ (a2 + adag2))
    )

    # Create drive operators
    drive_op1 = 2 * np.pi * r * (a0 + adag0)
    drive_op2 = 2 * np.pi * r * (a1 + adag1)
    drive_op3 = N2

    return Solver(
        static_hamiltonian=static_ham,
        hamiltonian_operators=[drive_op1, drive_op2, drive_op3],
        hamiltonian_channels=["d0", "d1", "u0"],
        channel_carrier_freqs={"d0": f0, "d1": f1, "u0": 0},
        dt=dt,
        array_library="numpy",
        rotating_frame=static_ham,
        in_frame_basis=False,
    )


def _get_basis_statevector(qubit_index: int, level: int, dim: int) -> Statevector:
    """Gets the state of the qubits and coupler, dim levels each, in which all are in their
    ground state but the qubit of the index, which is at the level"""
    vector = np.zeros(dim**3, dtype=complex)
    vector[level * dim**qubit_index] = 1
    return Statevector(vector)
