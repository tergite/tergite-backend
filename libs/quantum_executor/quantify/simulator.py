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
"""A simulated device that plays the pulses quantify compiles on QuTiP, in place of clusters

Each transmon a schedule touches is a three-level system in the frame of its drive clock,
driven at a Rabi rate that makes its calibrated pi pulse a pi rotation; a coupler's pulse
switches on a ZZ interaction of its two transmons, scaled so that its calibrated CZ pulse adds
a phase of pi to their |11>. T1 and T2 come from the calibration. An acquisition reads the
state at its start, without collapsing it, into IQ points about each qubit's state's point.
"""
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import numpy as np
import qutip
import xarray as xr
from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice
from quantify_scheduler.enums import BinMode
from quantify_scheduler.helpers.waveforms import get_waveform
from quantify_scheduler.operations.gate_library import CZ, Rxy
from quantify_scheduler.schedules.schedule import ScheduleBase

from libs.device_parameters import BackendConfig
from utils.exc import UnsupportedCircuitError

from .device import get_edge_names

LEVELS = 3
# the anharmonicity of a transmon whose calibration gives none, in hertz
DEFAULT_ANHARMONICITY = -200e6
# a shift of a clock's phase by p turns its later pulses by exp(PHASE_SIGN * i * p)
PHASE_SIGN = -1
MEASURED_LAST = "the simulator measures each qubit once, after what else it plays on it"


@dataclass(frozen=True)
class SimulatedQubit:
    """A transmon as the simulator models it"""

    frequency: float
    anharmonicity: float
    # the Rabi rate, in hertz, of a drive of amplitude 1
    rabi_rate: float
    t1: Optional[float]
    t2: Optional[float]
    readout_points: Tuple[complex, complex]
    readout_noise: float


@dataclass(frozen=True)
class SimulatedDevice:
    """A device's transmons, by element name, and the ZZ rate, in hertz per unit amplitude,
    of each coupler's pulse, by its port"""

    qubits: Dict[str, SimulatedQubit]
    zz_rates: Dict[str, float]
    dt: float


def get_simulated_device(
    device: QuantumDevice, backend_config: BackendConfig
) -> SimulatedDevice:
    """Gets the simulated device of the quantum device's transmons and edges, its drives and
    couplers scaled to what its calibrated gates play

    Args:
        device: the quantum device of the calibration, without a hardware config
        backend_config: the backend config, with its calibration and simulator section

    Returns:
        the simulated device
    """
    dt = backend_config.general_config.dt
    simulator = backend_config.simulator
    qubits = {}
    for name in device.elements():
        element = device.get_element(name)
        f01 = element.clock_freqs.f01()
        f12 = element.clock_freqs.f12()
        anharmonicity = f12 - f01 if f12 and not math.isnan(f12) else None
        point0, point1 = simulator.readout_iq[_get_qubit_key(backend_config, name)]
        decoherence = _get_decoherence(backend_config, name)
        qubits[name] = SimulatedQubit(
            frequency=f01,
            anharmonicity=anharmonicity or DEFAULT_ANHARMONICITY,
            rabi_rate=_get_rabi_rate(device, name, dt),
            t1=decoherence[0],
            t2=decoherence[1],
            readout_points=(complex(*point0), complex(*point1)),
            readout_noise=simulator.readout_noise,
        )
    zz_rates = {}
    for name in get_edge_names(device):
        parent, child = name.split("_")
        rate = _get_zz_rate(device, parent, child, dt)
        if rate is not None:
            zz_rates[f"{name}:fl"] = rate
    return SimulatedDevice(qubits=qubits, zz_rates=zz_rates, dt=dt)


def get_discriminators(device: SimulatedDevice) -> Dict[str, Dict[str, float]]:
    """Gets the linear discriminator of each transmon's readout, by the name of its qubit,
    e.g. q12: the line half way between its two states' IQ points

    Args:
        device: the simulated device

    Returns:
        each discriminator's coef_0, coef_1 and intercept, a point being 1 where
        coef_0 * I + coef_1 * Q + intercept > 0
    """
    discriminators = {}
    for name, qubit in device.qubits.items():
        point0, point1 = qubit.readout_points
        normal = point1 - point0
        middle = (point0 + point1) / 2
        discriminators[f"q{int(name.lstrip('q'))}"] = {
            "coef_0": normal.real,
            "coef_1": normal.imag,
            # from 0.0, so that a line through the origin has an intercept of 0.0, not -0.0
            "intercept": 0.0 - (normal.real * middle.real + normal.imag * middle.imag),
        }
    return discriminators


def run_schedule(
    compiled: ScheduleBase,
    device: SimulatedDevice,
    shots: int,
    rng: np.random.Generator,
) -> xr.Dataset:
    """Simulates a schedule compiled for the device, without its hardware, and reads out
    its acquisitions as clusters would

    Args:
        compiled: the schedule, compiled for the device's quantum device
        device: the simulated device
        shots: the shots to read out
        rng: the random generator of the readout's outcomes and noise

    Returns:
        the acquisitions, a data variable per channel: each shot's IQ point for an appending
        bin mode, their mean for an averaging one

    Raises:
        UnsupportedCircuitError: the simulator measures each qubit once, after what else
            it plays on it
    """
    events = _get_events(compiled, device)
    _check_measured_last(events)
    elements = sorted(events.elements)
    model = _Model.create(device, elements)
    states = _compute_states(model, events)
    return _read_out(events.acquisitions, states, model, shots, rng)


@dataclass
class _Events:
    """What a schedule plays, in samples of the device's dt"""

    pulses: List[Tuple[int, str, np.ndarray]] = field(default_factory=list)
    phases: List[Tuple[int, str, Optional[float]]] = field(default_factory=list)
    frequencies: List[Tuple[int, str, float]] = field(default_factory=list)
    acquisitions: List[Tuple[int, str, int, Any]] = field(default_factory=list)
    elements: Set[str] = field(default_factory=set)


def _get_events(compiled: ScheduleBase, device: SimulatedDevice) -> _Events:
    """Gets the pulses on the transmons' drives and the couplers, the changes of the drive
    clocks' phases and frequencies, and the acquisitions, of the compiled schedule"""
    events = _Events()
    for start, info in _get_timed_infos(compiled, 0.0):
        sample = round(start / device.dt)
        port, clock = info.get("port"), info.get("clock") or ""
        element = (port or clock).split(":")[0].split(".")[0]
        if "acq_channel" in info:
            events.acquisitions.append(
                (sample, element, info["acq_channel"], info.get("bin_mode"))
            )
            events.elements.add(element)
        elif info.get("wf_func") is not None and port:
            if port.endswith(":mw") or port in device.zz_rates:
                samples = get_waveform(info, sampling_rate=1 / device.dt)
                events.pulses.append((sample, port, np.asarray(samples, complex)))
                events.elements.update(port.split(":")[0].split("_"))
        elif clock.endswith(".01"):
            if "phase_shift" in info:
                events.phases.append(
                    (sample, element, math.radians(info["phase_shift"]))
                )
            elif info.get("reset_clock_phase"):
                events.phases.append((sample, element, None))
            elif "clock_freq_new" in info:
                events.frequencies.append((sample, element, info["clock_freq_new"]))
    events.elements &= set(device.qubits)
    return events


def _check_measured_last(events: _Events) -> None:
    """Checks that each transmon is acquired once, after anything else plays on it, as an
    acquisition does not collapse the state

    Raises:
        UnsupportedCircuitError: the simulator measures each qubit once, after what else
            it plays on it
    """
    acquired: Dict[str, int] = {}
    for start, name, _, _ in sorted(events.acquisitions):
        if name in acquired:
            raise UnsupportedCircuitError(MEASURED_LAST)
        acquired[name] = start
    for start, port, _ in events.pulses:
        names = port.split(":")[0].split("_")
        if any(start >= acquired.get(name, math.inf) for name in names):
            raise UnsupportedCircuitError(MEASURED_LAST)
    for start, name, _ in [*events.phases, *events.frequencies]:
        if start > acquired.get(name, math.inf):
            raise UnsupportedCircuitError(MEASURED_LAST)


def _get_timed_infos(
    schedule: ScheduleBase, offset: float
) -> Iterator[Tuple[float, Dict[str, Any]]]:
    """Gets each pulse and acquisition of the compiled schedule, those within it too, with
    its start in seconds"""
    for schedulable in schedule.schedulables.values():
        start = offset + schedulable["abs_time"]
        operation = schedule.operations[schedulable["operation_id"]]
        if isinstance(operation, ScheduleBase):
            yield from _get_timed_infos(operation, start)
            continue
        for info in operation.data.get("pulse_info", []):
            yield start + info.get("t0", 0.0), info
        for info in operation.data.get("acquisition_info", []):
            yield start + info.get("t0", 0.0), info


@dataclass(frozen=True)
class _Model:
    """The operators of the simulated transmons, on their joint space"""

    device: SimulatedDevice
    elements: List[str]
    annihilators: Dict[str, qutip.Qobj]
    numbers: Dict[str, qutip.Qobj]
    collapses: List[qutip.Qobj]

    @classmethod
    def create(cls, device: SimulatedDevice, elements: List[str]) -> "_Model":
        """Creates the operators of the transmons, each of LEVELS levels"""
        annihilators, numbers, collapses = {}, {}, []
        for index, name in enumerate(elements):
            factors = [qutip.qeye(LEVELS)] * len(elements)
            factors[index] = qutip.destroy(LEVELS)
            annihilator = qutip.tensor(factors) if elements else qutip.destroy(LEVELS)
            annihilators[name] = annihilator
            numbers[name] = annihilator.dag() * annihilator
            collapses.extend(_get_collapses(device.qubits[name], annihilator))
        return cls(device, elements, annihilators, numbers, collapses)

    def get_ground_state(self) -> qutip.Qobj:
        """Gets the state of every transmon in |0>"""
        kets = [qutip.basis(LEVELS, 0) for _ in self.elements]
        ket = qutip.tensor(kets) if kets else qutip.basis(1, 0)
        return ket * ket.dag()

    def get_static_hamiltonian(self, detunings: Dict[str, float]) -> qutip.Qobj:
        """Gets the transmons' Hamiltonian in the frames of their drive clocks, each detuned
        from its transmon by the given hertz"""
        hamiltonian = 0 * self.get_ground_state()
        for name in self.elements:
            number = self.numbers[name]
            qubit = self.device.qubits[name]
            identity = qutip.qeye(number.dims[0])
            hamiltonian += 2 * math.pi * detunings[name] * number
            hamiltonian += math.pi * qubit.anharmonicity * number * (number - identity)
        return hamiltonian


def _get_collapses(qubit: SimulatedQubit, annihilator: qutip.Qobj) -> List[qutip.Qobj]:
    """Gets the collapse operators of the transmon's T1 and of its pure dephasing"""
    collapses = []
    relaxation = 1 / qubit.t1 if qubit.t1 else 0.0
    if relaxation:
        collapses.append(math.sqrt(relaxation) * annihilator)
    dephasing = (1 / qubit.t2 - relaxation / 2) if qubit.t2 else 0.0
    if dephasing > 0:
        collapses.append(math.sqrt(2 * dephasing) * annihilator.dag() * annihilator)
    return collapses


def _compute_states(model: _Model, events: _Events) -> Dict[int, qutip.Qobj]:
    """Evolves the transmons from |0> through the events, and gets their state at each
    acquisition's start, by its sample"""
    boundaries = _get_boundaries(events)
    phases = {name: 0.0 for name in model.elements}
    detunings = {name: 0.0 for name in model.elements}
    state = model.get_ground_state()
    states = {}
    for start, end in zip(boundaries, boundaries[1:] + [boundaries[-1]]):
        _apply_clock_changes(events, start, phases, detunings, model)
        states[start] = state
        if end > start and model.elements:
            state = _compute_window_state(
                model, events, start, end, phases, detunings, state
            )
    return states


def _get_boundaries(events: _Events) -> List[int]:
    """Gets the samples at which what drives the transmons changes, or an acquisition
    starts"""
    boundaries = {0}
    for start, _, samples in events.pulses:
        boundaries.update((start, start + len(samples)))
    boundaries.update(start for start, _, _ in events.phases)
    boundaries.update(start for start, _, _ in events.frequencies)
    boundaries.update(start for start, _, _, _ in events.acquisitions)
    return sorted(boundaries)


def _apply_clock_changes(
    events: _Events,
    sample: int,
    phases: Dict[str, float],
    detunings: Dict[str, float],
    model: _Model,
) -> None:
    """Applies the changes of the drive clocks' phases and frequencies at the sample"""
    for start, name, phase in events.phases:
        if start == sample and name in phases:
            phases[name] = 0.0 if phase is None else phases[name] + phase
    for start, name, frequency in events.frequencies:
        if start == sample and name in detunings:
            detunings[name] = model.device.qubits[name].frequency - frequency


def _compute_window_state(
    model: _Model,
    events: _Events,
    start: int,
    end: int,
    phases: Dict[str, float],
    detunings: Dict[str, float],
    state: qutip.Qobj,
) -> qutip.Qobj:
    """Evolves the state over the samples from the start to the end, in which nothing but
    the pulses changes, each sample's drive held for its dt"""
    dt = model.device.dt
    times = np.arange(start, end + 1) * dt
    hamiltonian: List[Any] = [model.get_static_hamiltonian(detunings)]
    for port, samples in _get_window_samples(events, start, end).items():
        hamiltonian.extend(_get_drive_terms(model, port, samples, phases, times))
    if len(hamiltonian) == 1:
        return _compute_idle_state(model, hamiltonian[0], state, (end - start) * dt)
    result = qutip.mesolve(
        qutip.QobjEvo(hamiltonian),
        state,
        times,
        c_ops=model.collapses,
        options={"store_states": False, "store_final_state": True},
    )
    return result.final_state


def _compute_idle_state(
    model: _Model, hamiltonian: qutip.Qobj, state: qutip.Qobj, duration: float
) -> qutip.Qobj:
    """Evolves the state for the seconds under a constant Hamiltonian, exactly, as an ODE
    solver would take a step for each swing of the third level's phase over microseconds
    of waiting"""
    propagator = (qutip.liouvillian(hamiltonian, model.collapses) * duration).expm()
    evolved = qutip.vector_to_operator(propagator * qutip.operator_to_vector(state))
    return (evolved + evolved.dag()) / 2


def _get_window_samples(events: _Events, start: int, end: int) -> Dict[str, np.ndarray]:
    """Gets what each port plays in each sample from the start to the end"""
    window: Dict[str, np.ndarray] = {}
    for pulse_start, port, samples in events.pulses:
        first, last = max(start, pulse_start), min(end, pulse_start + len(samples))
        if first >= last:
            continue
        played = window.setdefault(port, np.zeros(end - start, complex))
        played[first - start : last - start] += samples[
            first - pulse_start : last - pulse_start
        ]
    return window


def _get_drive_terms(
    model: _Model,
    port: str,
    samples: np.ndarray,
    phases: Dict[str, float],
    times: np.ndarray,
) -> List[Any]:
    """Gets the time-dependent terms of what the port plays: a transmon's drive, turned by
    its clock's phase, or a coupler's ZZ interaction"""
    held = np.append(samples, samples[-1])
    if port.endswith(":mw"):
        name = port.split(":")[0]
        if name not in model.annihilators:
            return []
        annihilator = model.annihilators[name]
        rate = math.pi * model.device.qubits[name].rabi_rate
        turned = held * np.exp(1j * PHASE_SIGN * phases[name])
        return [
            [rate * annihilator.dag(), qutip.coefficient(turned, tlist=times, order=0)],
            [
                rate * annihilator,
                qutip.coefficient(turned.conj(), tlist=times, order=0),
            ],
        ]
    parent, child = port.split(":")[0].split("_")
    if parent not in model.numbers or child not in model.numbers:
        return []
    rate = 2 * math.pi * model.device.zz_rates[port]
    interaction = -rate * model.numbers[parent] * model.numbers[child]
    return [[interaction, qutip.coefficient(np.abs(held), tlist=times, order=0)]]


def _read_out(
    acquisitions: Sequence[Tuple[int, str, int, Any]],
    states: Dict[int, qutip.Qobj],
    model: _Model,
    shots: int,
    rng: np.random.Generator,
) -> xr.Dataset:
    """Reads out each acquisition: each shot's outcome sampled, with the others at the same
    sample, from the state then, a level above 0 read as 1, into a noisy IQ point"""
    outcomes = _generate_outcomes(acquisitions, states, model, shots, rng)
    variables = {}
    for (_, name, channel, bin_mode), bits in zip(acquisitions, outcomes):
        qubit = model.device.qubits[name]
        points = np.where(bits, *reversed(qubit.readout_points))
        noise = rng.normal(0, qubit.readout_noise, (shots, 2)) @ np.array([1, 1j])
        values = points + noise
        index = f"acq_index_{channel}"
        if bin_mode == BinMode.AVERAGE:
            variables[channel] = xr.DataArray([values.mean()], dims=[index])
        else:
            variables[channel] = xr.DataArray(
                values[:, np.newaxis], dims=["repetition", index]
            )
    return xr.Dataset(variables)


def _generate_outcomes(
    acquisitions: Sequence[Tuple[int, str, int, Any]],
    states: Dict[int, qutip.Qobj],
    model: _Model,
    shots: int,
    rng: np.random.Generator,
) -> List[np.ndarray]:
    """Samples each shot's outcome of each acquisition, jointly with the others at its
    sample"""
    outcomes: List[Optional[np.ndarray]] = [None] * len(acquisitions)
    for sample in sorted({start for start, _, _, _ in acquisitions}):
        group = [i for i, acq in enumerate(acquisitions) if acq[0] == sample]
        names = [acquisitions[i][1] for i in group]
        joint = _get_joint_probabilities(states[sample], model, names)
        drawn = rng.choice(len(joint), size=shots, p=joint)
        for position, index in enumerate(group):
            outcomes[index] = (drawn >> (len(names) - 1 - position)) & 1
    return outcomes


def _get_joint_probabilities(
    state: qutip.Qobj, model: _Model, names: Sequence[str]
) -> np.ndarray:
    """Gets the probability of each outcome of the named transmons, the first the most
    significant bit, a level above 0 read as 1"""
    populations = np.clip(np.real(state.diag()), 0, None)
    levels = np.array(list(np.ndindex(*([LEVELS] * len(model.elements))))).reshape(
        -1, len(model.elements)
    )
    columns = [model.elements.index(name) for name in names]
    bits = (levels[:, columns] > 0).astype(int)
    codes = bits @ (1 << np.arange(len(names))[::-1])
    joint = np.bincount(codes, weights=populations, minlength=1 << len(names))
    return joint / joint.sum()


def _get_qubit_key(backend_config: BackendConfig, element: str) -> str:
    """Gets the id the backend config gives the element's qubit, e.g. q0 of q00"""
    number = int(element.lstrip("q"))
    for qubit_id in backend_config.device_config.qubit_ids:
        if int(qubit_id.lstrip("q")) == number:
            return qubit_id
    return element


def _get_decoherence(
    backend_config: BackendConfig, element: str
) -> Tuple[Optional[float], Optional[float]]:
    """Gets the element's qubit's T1 and T2 in seconds, from the calibration; none of
    either it lacks"""
    number = int(element.lstrip("q"))
    for qubit in backend_config.calibration_config.qubit:
        if int(str(qubit["id"]).lstrip("q")) == number:
            return qubit.get("t1_decoherence"), qubit.get("t2_decoherence")
    return None, None


def _get_rabi_rate(device: QuantumDevice, element: str, dt: float) -> float:
    """Gets the Rabi rate of a drive of amplitude 1 that makes the transmon's calibrated
    pi pulse a pi rotation"""
    rxy = Rxy(theta=180, phi=0, qubit=element)
    samples = _get_compiled_samples(device, rxy, ":mw", dt)
    return 1 / (2 * np.real(samples).sum() * dt)


def _get_zz_rate(
    device: QuantumDevice, parent: str, child: str, dt: float
) -> Optional[float]:
    """Gets the ZZ rate of a coupler pulse of amplitude 1 that makes the calibrated CZ's
    pulse add a phase of pi; none for a CZ that plays no pulse on its coupler"""
    samples = _get_compiled_samples(device, CZ(parent, child), ":fl", dt)
    area = np.abs(samples).sum() * dt
    return 1 / (2 * area) if area else None


def _get_compiled_samples(
    device: QuantumDevice, operation: Any, suffix: str, dt: float
) -> np.ndarray:
    """Gets the samples the operation, compiled on the device, plays on its port of the
    suffix, one each dt"""
    schedule = Schedule("calibrated")
    schedule.add(operation)
    compiled = SerialCompiler("calibrated").compile(
        schedule, config=device.generate_compilation_config()
    )
    played = [
        get_waveform(info, sampling_rate=1 / dt)
        for _, info in _get_timed_infos(compiled, 0.0)
        if info.get("wf_func") is not None and str(info.get("port")).endswith(suffix)
    ]
    return np.concatenate(played) if played else np.zeros(0)
