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
from qiskit_dynamics import DynamicsBackend, Solver

from app.libs.device_parameters import BackendConfig
from app.libs.quantum_executor.qiskit.functions import (
    fit_lda,
    get_basis_statevector,
    get_lo_range,
    omega_c,
)

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


def train_two_qubit_discriminator(backend: DynamicsBackend, shots: int = 1024) -> dict:
    """Generates |0> and |1> states for each qubit, and trains a linear discriminator on them

    Args:
        backend: the backend to simulate the states on
        shots: number of shots for generating data

    Returns:
        Discriminator object as json in the format to store it in the database
    """
    dims = backend.options.subsystem_dims
    lda_results = {}
    for qubit_idx in [x[0] for x in backend.configuration().meas_map]:
        schedule = Schedule((0, Acquire(1, AcquireChannel(qubit_idx), MemorySlot(0))))
        iq_values = [
            backend.run(
                [schedule],
                shots=shots,
                initial_state=get_basis_statevector(dims, mode=qubit_idx, level=level),
            )
            .result()
            .data()["memory"]
            .reshape(shots, 2)
            for level in (0, 1)
        ]
        lda_results[f"q{qubit_idx}"] = fit_lda(*iq_values)
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
    operators = _get_ladder_operators(dim)
    static_ham = _get_static_hamiltonian(params, operators)
    return Solver(
        static_hamiltonian=static_ham,
        hamiltonian_operators=_get_drive_operators(operators, r=r),
        hamiltonian_channels=["d0", "d1", "u0"],
        channel_carrier_freqs={"d0": params.f0, "d1": params.f1, "u0": 0},
        dt=dt,
        array_library="numpy",
        rotating_frame=static_ham,
        in_frame_basis=False,
    )


@dataclass(frozen=True)
class _LadderOperators:
    """The annihilation, creation and number operators of qubit 0, qubit 1 and the
    coupler, in that order, on their joint space"""

    annihilators: tuple[np.ndarray, np.ndarray, np.ndarray]
    creators: tuple[np.ndarray, np.ndarray, np.ndarray]
    numbers: tuple[np.ndarray, np.ndarray, np.ndarray]


def _get_ladder_operators(dim: int) -> _LadderOperators:
    """Gets the ladder operators of the two qubits and the coupler, dim levels each"""
    annihilator = np.diag(np.sqrt(np.arange(1, dim)), 1)
    creator = np.diag(np.sqrt(np.arange(1, dim)), -1)
    number = np.diag(np.arange(dim))
    modes = range(3)
    return _LadderOperators(
        annihilators=tuple(
            _get_mode_operator(annihilator, mode, dim) for mode in modes
        ),
        creators=tuple(_get_mode_operator(creator, mode, dim) for mode in modes),
        numbers=tuple(_get_mode_operator(number, mode, dim) for mode in modes),
    )


def _get_static_hamiltonian(
    params: _TransmonParams, operators: _LadderOperators
) -> np.ndarray:
    """Gets the Hamiltonian of the two transmons and the coupler, at the coupler's
    frequency at the start of its CZ pulse"""
    a0, a1, a2 = operators.annihilators
    adag0, adag1, adag2 = operators.creators
    n0, n1, n2 = operators.numbers
    ident = np.eye(n0.shape[0], dtype=complex)
    coupler = params.coupler
    coupler_frequency = omega_c(
        0,
        delta_0=coupler["cz_pulse_amplitude"],  # Maximum delta value
        theta=coupler["cz_pulse_dc_bias"],  # DC bias term
        omega_c0=2 * np.pi * params.f2,  # Maximum frequency in Hz
        omega_phi=coupler["frequency_detuning"]
        + 2 * np.pi * (params.f1 - params.f0 - params.alpha0),  # Transition frequency
        phi=coupler["cz_pulse_phase_offset"],  # Phase offset
        t_w=coupler["cz_pulse_duration_before"],  # s, duration before pulse
        t_rf=coupler["cz_pulse_duration_rise"],  # s, rise time
        t_p=coupler["cz_pulse_duration_constant"],  # s, constant pulse duration
    )
    return (
        2 * np.pi * params.f0 * n0
        + np.pi * params.alpha0 * n0 @ (n0 - ident)
        + 2 * np.pi * params.f1 * n1
        + np.pi * params.alpha1 * n1 @ (n1 - ident)
        + coupler_frequency * n2
        + np.pi * params.alpha2 * n2 @ (n2 - ident)
        + 2 * np.pi * params.g02 * ((a0 + adag0) @ (a2 + adag2))
        + 2 * np.pi * params.g12 * ((a1 + adag1) @ (a2 + adag2))
    )


def _get_drive_operators(operators: _LadderOperators, r: float) -> list[np.ndarray]:
    """Gets the operators that the channels d0, d1 and u0 drive: the two qubits at the
    Rabi rate r, in Hz, and the coupler's number"""
    a0, a1, _ = operators.annihilators
    adag0, adag1, _ = operators.creators
    return [
        2 * np.pi * r * (a0 + adag0),
        2 * np.pi * r * (a1 + adag1),
        operators.numbers[2],
    ]


def _get_mode_operator(operator: np.ndarray, mode: int, dim: int) -> np.ndarray:
    """Gets the operator of one mode on the joint space of the three, whose coupler, mode
    2, is the most significant"""
    factors = [np.eye(dim)] * 3
    factors[2 - mode] = operator
    return np.kron(factors[0], np.kron(factors[1], factors[2]))
