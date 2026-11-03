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
"""Checks that a device's configs can run gate-level jobs, without its instruments

Usage
=====

From the backend's folder, with the quantify extra installed, and the env file of each device
as ``bcc start`` takes them:

    uv run python -m dev.check_gate_level loke.env thor.env

For each device the quantify executor runs, it reports as errors what would fail its
gate-level jobs or their counts, and as warnings what leaves them a gate fewer, or the device
without pulse-level jobs; it exits 1 if it found an error. It compiles one circuit for each port and clock that gate-level jobs play,
as a job does, so what the compiler refuses of the hardware config shows; and it checks the
frequency each sequencer's NCO would get, which only the instrument refuses.
"""
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Sequence, Set

from quantify_scheduler.backends import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

from libs.device_parameters import BackendConfig, DeviceCalibration
from libs.device_parameters.dtos import CalibrationValue, CouplerCalibration
from libs.quantum_executor.base import gates
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from libs.quantum_executor.quantify.circuits import (
    get_circuit_schedule,
    get_measure_duration,
)
from libs.quantum_executor.quantify.device import (
    apply_calibration,
    get_edge_names,
    get_element_name,
    remove_elements,
)
from libs.quantum_executor.quantify.utils.config import load_quantify_config
from utils.config import DeviceSettings, read_device_settings

# the frequencies a Qblox sequencer's NCO may be set to, either way
NCO_LIMIT_HZ = 500e6
# the qubit fields gate-level jobs compile their X, SX and RX from
REQUIRED_QUBIT_FIELDS = ("frequency", "pi_pulse_amplitude", "pi_pulse_duration")
# the readout resonator fields a gate-level measurement is played and acquired with
REQUIRED_RESONATOR_FIELDS = (
    "frequency",
    "pulse_amplitude",
    "pulse_duration",
    "acq_integration_time",
)
LDA_FIELDS = ("coef_0", "coef_1", "intercept")


@dataclass(frozen=True)
class Finding:
    """What the check found of a transmon, a coupler or the device, named as the hardware
    config names them: an error fails gate-level jobs or their counts, a warning leaves
    them a gate fewer"""

    is_error: bool
    subject: str
    message: str


def check_device(settings: DeviceSettings) -> List[Finding]:
    """Checks the configs a device's settings name for gate-level jobs

    Args:
        settings: the device's settings, as read from its env file

    Returns:
        what the check found; nothing for a device the simulators run, which compile
        against their own models
    """
    if settings.executor_type != "quantify":
        return []
    backend_config = BackendConfig.from_toml(
        settings.backend_settings, seed_file=settings.calibration_seed
    )
    hardware_config = load_quantify_config(settings.quantify_config_file)
    return check_configs(backend_config, hardware_config)


def check_configs(backend_config: BackendConfig, hardware_config: Any) -> List[Finding]:
    """Checks a device's backend config, with its calibration, and its quantify hardware
    config for gate-level jobs

    The compilation of a transmon or coupler whose calibration falls short is not
    checked, as its errors would only repeat that.

    Args:
        backend_config: the device's backend config, with its calibration seed
        hardware_config: the device's quantify hardware config

    Returns:
        what the check found
    """
    if backend_config.calibration_config is None:
        return [
            Finding(
                True, "device", "has no calibration, which gate-level jobs compile from"
            )
        ]
    calibration = DeviceCalibration.from_config(backend_config)
    findings = [
        *_check_qubits(backend_config, calibration),
        *_check_couplers(backend_config, calibration),
    ]
    if backend_config.pulse_limits is None:
        findings.append(
            Finding(
                False,
                "device",
                "sets no pulse_limits in its backend config, so it refuses pulse-level "
                "jobs",
            )
        )
    subjects_with_errors = {finding.subject for finding in findings if finding.is_error}
    return [
        *findings,
        *_check_compilation(
            backend_config, hardware_config, calibration, subjects_with_errors
        ),
    ]


def main(env_files: Sequence[str]) -> int:
    """Checks the devices of the env files, printing what it found of each

    Args:
        env_files: the paths to the devices' env files

    Returns:
        1 if it found an error, else 0
    """
    has_errors = False
    for env_file in env_files:
        settings = read_device_settings(Path(env_file).resolve())
        findings = check_device(settings)
        errors = sum(finding.is_error for finding in findings)
        print(
            f"{env_file} ({settings.executor_type}): {errors} errors, "
            f"{len(findings) - errors} warnings"
        )
        for finding in findings:
            level = "error  " if finding.is_error else "warning"
            print(f"  {level} {finding.subject}: {finding.message}")
        has_errors = has_errors or errors > 0
    return 1 if has_errors else 0


def _check_qubits(
    backend_config: BackendConfig, calibration: DeviceCalibration
) -> Iterator[Finding]:
    """The calibration each qubit's gates, readout and counts need, that it lacks"""
    qubits = {qubit.id: qubit for qubit in calibration.qubits}
    resonators = {resonator.id: resonator for resonator in calibration.resonators or []}
    lda = (calibration.discriminators or {}).get("lda", {})
    for qubit_id in backend_config.device_config.qubit_numbers:
        name = get_element_name(qubit_id)
        qubit = qubits.get(qubit_id)
        if qubit is None:
            yield Finding(True, name, "has no calibration")
            continue
        for field in _get_missing_fields(qubit, REQUIRED_QUBIT_FIELDS):
            yield Finding(True, name, f"has no {field}, which its X gates need")
        resonator = resonators.get(qubit_id)
        if resonator is None:
            yield Finding(True, name, "has no readout resonator calibration")
        else:
            for field in _get_missing_fields(resonator, REQUIRED_RESONATOR_FIELDS):
                yield Finding(
                    True, name, f"has no resonator {field}, which its readout needs"
                )
        lda_key = f"q{qubit_id}"
        missing_lda = [f for f in LDA_FIELDS if f not in lda.get(lda_key, {})]
        if missing_lda:
            yield Finding(
                True,
                name,
                f"has no {', '.join(missing_lda)} in discriminators.lda.{lda_key}, so its "
                "counts (meas_level 2) cannot be discriminated",
            )


def _check_couplers(
    backend_config: BackendConfig, calibration: DeviceCalibration
) -> Iterator[Finding]:
    """The coupled pairs that get no CZ, and the couplers whose CZ cannot be played or
    plays no pulse"""
    couplers = {
        _get_qubit_pair(coupler.full_name): coupler
        for coupler in calibration.couplers or []
    }
    for first, second in backend_config.device_config.coupling_dict.values():
        pair = _get_qubit_pair(f"{first}_{second}")
        name = "_".join(get_element_name(qubit_id) for qubit_id in sorted(pair))
        coupler = couplers.get(pair)
        if coupler is None:
            yield Finding(False, name, "has no calibration, so its qubits get no cz")
        elif _get_value(coupler.frequency) is None:
            yield Finding(True, name, "has no frequency, which its CZ is played at")
        elif _get_cz_pulse_duration(coupler) <= 0 or not _get_value(
            coupler.cz_pulse_amplitude
        ):
            yield Finding(
                True,
                name,
                "is calibrated with no CZ pulse, so the cz the SDK offers on its qubits "
                "only shifts their phases",
            )


def _check_compilation(
    backend_config: BackendConfig,
    hardware_config: Any,
    calibration: DeviceCalibration,
    subjects_with_errors: Set[str],
) -> Iterator[Finding]:
    """What the compiler refuses of each port and clock gate-level jobs play, and the NCO
    frequencies beyond what a sequencer takes, but of the subjects with errors already
    """
    qubit_ids = backend_config.device_config.qubit_numbers
    element_names = [get_element_name(qubit_id) for qubit_id in qubit_ids]
    lo_frequencies = _get_lo_frequencies(hardware_config)
    qubits = {qubit.id: qubit for qubit in calibration.qubits}
    resonators = {resonator.id: resonator for resonator in calibration.resonators or []}

    device = QuantumDevice(f"{backend_config.general_config.name}_gate_level_check")
    device.hardware_config(hardware_config)
    apply_calibration(device, backend_config)
    try:
        for index, (qubit_id, name) in enumerate(zip(qubit_ids, element_names)):
            if name in subjects_with_errors:
                continue
            yield from _check_port_clock(
                device,
                element_names,
                native_gates=[gates.Rxy(index, math.pi, 0.0)],
                port_clock=f"{name}:mw-{name}.01",
                frequency=_get_value(qubits[qubit_id].frequency),
                lo_frequencies=lo_frequencies,
            )
            yield from _check_port_clock(
                device,
                element_names,
                native_gates=[gates.Measure(index, clbit=0, channel=0)],
                port_clock=f"{name}:res-{name}.ro",
                frequency=_get_value(resonators[qubit_id].frequency),
                lo_frequencies=lo_frequencies,
            )
        for edge in get_edge_names(device):
            parent, child = edge.split("_")
            if {edge, parent, child} & subjects_with_errors:
                continue
            coupler = next(
                c
                for c in calibration.couplers
                if _get_qubit_pair(c.full_name) == _get_qubit_pair(edge)
            )
            yield from _check_port_clock(
                device,
                element_names,
                native_gates=[
                    gates.CZ(element_names.index(parent), element_names.index(child))
                ],
                port_clock=f"{edge}:fl-{edge}.cz",
                frequency=_get_value(coupler.frequency),
                lo_frequencies=lo_frequencies,
            )
    finally:
        remove_elements(device)
        device.close()


def _check_port_clock(
    device: QuantumDevice,
    element_names: List[str],
    native_gates: List[gates.Gate],
    port_clock: str,
    frequency: Optional[float],
    lo_frequencies: Dict[str, float],
) -> Iterator[Finding]:
    """What the compiler refuses of a circuit of the gates, on the transmons of the names,
    which plays the port and clock at the frequency, and its NCO frequencies beyond a
    sequencer's, naming the LO where the hardware config sets the port and clock one"""
    subject = port_clock.split(":")[0]
    try:
        nco_frequencies = _get_nco_frequencies(device, element_names, native_gates)
    except Exception as exp:
        yield Finding(True, subject, f"does not compile on {port_clock}: {exp}")
        return

    lo_frequency = lo_frequencies.get(port_clock)
    if lo_frequency is not None and frequency is not None:
        nco_frequency = frequency - lo_frequency
        if abs(nco_frequency) > NCO_LIMIT_HZ:
            yield Finding(
                True,
                subject,
                f"the LO of {port_clock} is {lo_frequency / 1e6:.1f} MHz, so its NCO would "
                f"be {nco_frequency / 1e6:.1f} MHz, beyond ±{NCO_LIMIT_HZ / 1e6:.0f} MHz; "
                f"set the LO within {NCO_LIMIT_HZ / 1e6:.0f} MHz of its "
                f"{frequency / 1e6:.1f} MHz",
            )
            return
    for nco_frequency in nco_frequencies:
        if abs(nco_frequency) > NCO_LIMIT_HZ:
            yield Finding(
                True,
                subject,
                f"compiles {port_clock} to an NCO of {nco_frequency / 1e6:.1f} MHz, beyond "
                f"±{NCO_LIMIT_HZ / 1e6:.0f} MHz",
            )


def _get_nco_frequencies(
    device: QuantumDevice, element_names: List[str], native_gates: List[gates.Gate]
) -> List[float]:
    """Compiles a circuit of the gates, its qubit i on the transmon of the i-th name, as a
    gate-level job does, and gets the NCO frequency of each sequencer it plays on"""
    circuit = gates.NativeCircuit(
        name="check",
        num_qubits=len(element_names),
        num_clbits=1,
        gates=tuple(native_gates),
    )
    native_config = NativeQobjConfig.from_measurement_settings(
        meas_level=2, meas_return="single", shots=1, n_qubits=len(element_names)
    )
    measure_durations = {
        name: get_measure_duration(
            measure.pulse_duration(), measure.acq_delay(), measure.integration_time()
        )
        for name in element_names
        for measure in [device.get_element(name).measure]
    }
    schedule = get_circuit_schedule(
        circuit,
        native_config,
        element_names=element_names,
        edge_names=get_edge_names(device),
        measure_durations=measure_durations,
    )
    compiled = SerialCompiler(name="gate_level_check").compile(
        schedule, config=device.generate_compilation_config()
    )
    return [
        sequencer.modulation_freq
        for instrument in compiled.compiled_instructions.values()
        if isinstance(instrument, dict)
        for module in instrument.values()
        if isinstance(module, dict)
        for sequencer in module.get("sequencers", {}).values()
        if getattr(sequencer, "modulation_freq", None) is not None
    ]


def _get_lo_frequencies(hardware_config: Any) -> Dict[str, float]:
    """The LO frequency the hardware config sets for each port and clock that has one"""
    modulation_frequencies = (
        getattr(hardware_config.hardware_options, "modulation_frequencies", None) or {}
    )
    return {
        port_clock: float(entry.lo_freq)
        for port_clock, entry in modulation_frequencies.items()
        if getattr(entry, "lo_freq", None) is not None
    }


def _get_qubit_pair(name: Optional[str]) -> FrozenSet[int]:
    """The numbers of the two qubits a coupler's name joins, e.g. {6, 7} for q06_q07"""
    if not name:
        return frozenset()
    return frozenset(int(qubit.lstrip("q")) for qubit in name.split("_"))


def _get_missing_fields(calibration: Any, fields: Sequence[str]) -> List[str]:
    """The fields of the calibration that have no number"""
    return [
        field for field in fields if _get_value(getattr(calibration, field)) is None
    ]


def _get_cz_pulse_duration(coupler: CouplerCalibration) -> float:
    """The duration of the coupler's CZ pulse, its rise and constant part, in seconds"""
    return (_get_value(coupler.cz_pulse_duration_rise) or 0.0) + (
        _get_value(coupler.cz_pulse_duration_constant) or 0.0
    )


def _get_value(value: Optional[CalibrationValue]) -> Optional[float]:
    """The calibration value's number, or None if it has none"""
    if value is None or value.value is None:
        return None
    number = float(value.value)
    return None if math.isnan(number) else number


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
