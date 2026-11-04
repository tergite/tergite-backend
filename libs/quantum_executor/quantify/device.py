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
#
# The coupler edge follows qpi-driver's `FluxTunableCoupler`
# (https://github.com/sopherapps/qpi, MIT License, (C) Copyright Martin Ahindura 2026).
"""The quantify device that gate-level jobs compile against: a transmon for each qubit and
an edge for each coupler, set from the backend's calibration"""

import math
from typing import Any, Dict, List, Optional, Tuple

from qcodes import Instrument, ManualParameter
from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import OperationCompilationConfig
from quantify_scheduler.device_under_test.edge import Edge
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice
from quantify_scheduler.device_under_test.transmon_element import BasicTransmonElement
from quantify_scheduler.operations.pulse_library import (
    IdlePulse,
    ShiftClockPhase,
    SoftSquarePulse,
)
from quantify_scheduler.resources import ClockResource

from libs.device_parameters import BackendConfig, DeviceCalibration
from libs.device_parameters.dtos import (
    CalibrationValue,
    CouplerCalibration,
    QubitCalibration,
    ResonatorCalibration,
)

# the time grid the Qblox instruments play on, in seconds
QBLOX_TIMEGRID_INTERVAL = 4e-9
# how long a reset waits for the qubit to relax, as the SDK's reset did
RESET_DURATION = 300e-6


class CouplerEdge(Edge):
    """A tunable coupler between two transmons, named {parent}_{child}, whose CZ is a soft
    square pulse on its flux port at its frequency, between two waits, followed by a phase
    correction of each transmon"""

    def __init__(
        self, parent_element_name: str, child_element_name: str, **kwargs: Any
    ) -> None:
        super().__init__(parent_element_name, child_element_name, **kwargs)
        self.cz_amplitude = ManualParameter(
            "cz_amplitude", instrument=self, initial_value=0.0
        )
        self.cz_duration = ManualParameter(
            "cz_duration", instrument=self, unit="s", initial_value=0.0
        )
        self.cz_wait = ManualParameter(
            "cz_wait", instrument=self, unit="s", initial_value=0.0
        )
        self.cz_frequency = ManualParameter(
            "cz_frequency", instrument=self, unit="Hz", initial_value=0.0
        )
        self.parent_phase_correction = ManualParameter(
            "parent_phase_correction", instrument=self, unit="deg", initial_value=0.0
        )
        self.child_phase_correction = ManualParameter(
            "child_phase_correction", instrument=self, unit="deg", initial_value=0.0
        )

    def generate_edge_config(self) -> Dict[str, Dict[str, OperationCompilationConfig]]:
        """Gets the compilation config of this edge's CZ

        Returns:
            the CZ's compilation config, keyed by this edge's name
        """
        return {
            self.name: {
                "CZ": OperationCompilationConfig(
                    factory_func=get_cz_schedule,
                    factory_kwargs={
                        "square_amp": self.cz_amplitude(),
                        "square_duration": self.cz_duration(),
                        "square_port": f"{self.name}:fl",
                        "square_clock": f"{self.name}.cz",
                        "square_freq": self.cz_frequency(),
                        "wait_duration": self.cz_wait(),
                        "parent_phase": self.parent_phase_correction(),
                        "parent_clock": f"{self._parent_element_name}.01",
                        "child_phase": self.child_phase_correction(),
                        "child_clock": f"{self._child_element_name}.01",
                    },
                )
            }
        }


def get_cz_schedule(
    square_amp: float,
    square_duration: float,
    square_port: str,
    square_clock: str,
    square_freq: float,
    wait_duration: float,
    parent_phase: float,
    parent_clock: str,
    child_phase: float,
    child_clock: str,
) -> Schedule:
    """Gets the schedule of a coupler's CZ: a wait, the soft square pulse, a wait again,
    the phase corrections of the two transmons, and a grid interval before the next phase
    update; a pulse of no duration is left out

    Args:
        square_amp: the pulse's amplitude
        square_duration: the pulse's duration, in seconds
        square_port: the coupler's flux port
        square_clock: the coupler's clock
        square_freq: the frequency of the coupler's clock, in Hz
        wait_duration: the wait before and after the pulse, in seconds
        parent_phase: the phase correction of the parent transmon, in degrees
        parent_clock: the parent transmon's drive clock
        child_phase: the phase correction of the child transmon, in degrees
        child_clock: the child transmon's drive clock

    Returns:
        the schedule of the CZ
    """
    schedule = Schedule("CZ")
    if square_duration > 0:
        schedule.add_resource(ClockResource(name=square_clock, freq=square_freq))
        if wait_duration > 0:
            schedule.add(IdlePulse(duration=wait_duration))
        schedule.add(
            SoftSquarePulse(
                amp=square_amp,
                duration=square_duration,
                port=square_port,
                clock=square_clock,
            )
        )
        if wait_duration > 0:
            schedule.add(IdlePulse(duration=wait_duration))
    schedule.add(ShiftClockPhase(phase_shift=parent_phase, clock=parent_clock))
    schedule.add(ShiftClockPhase(phase_shift=child_phase, clock=child_clock))
    # a Qblox sequencer updates a clock's phase at most once per grid interval
    schedule.add(IdlePulse(duration=QBLOX_TIMEGRID_INTERVAL))
    return schedule


def get_element_name(qubit_id: int) -> str:
    """Gets the name of the qubit's transmon, which its ports and clocks carry, e.g. q06
    for qubit 6, as the hardware config names them

    Args:
        qubit_id: the number in the qubit's id

    Returns:
        the transmon's name
    """
    return f"q{qubit_id:02d}"


def apply_calibration(device: QuantumDevice, backend_config: BackendConfig) -> None:
    """Sets the device's transmons and coupler edges from the backend's calibration, the
    device holding those alone, whatever it held before; a device without a calibration
    is left without them

    Args:
        device: the quantum device that gate-level jobs compile against
        backend_config: the backend's config, with its calibration
    """
    transmons: List[BasicTransmonElement] = []
    edges: List[CouplerEdge] = []
    if backend_config.calibration_config is not None:
        calibration = DeviceCalibration.from_config(backend_config)
        resonators = {
            resonator.id: resonator for resonator in calibration.resonators or []
        }
        for qubit in calibration.qubits:
            transmon = _get_transmon(get_element_name(qubit.id))
            _set_transmon(transmon, qubit, resonators.get(qubit.id))
            transmons.append(transmon)

        qubit_ids = {qubit.id for qubit in calibration.qubits}
        for coupler in calibration.couplers or []:
            parent_id, child_id = _get_coupler_qubit_ids(coupler)
            if not {parent_id, child_id} <= qubit_ids:
                continue
            edge = _get_edge(get_element_name(parent_id), get_element_name(child_id))
            _set_edge(
                edge, coupler, is_parent_control=_is_parent_control(coupler, parent_id)
            )
            edges.append(edge)
    _set_elements(device, transmons, edges)


def remove_elements(device: QuantumDevice) -> None:
    """Closes the device's coupler edges and transmons, and takes them off it, so that
    another instrument may take their names, as tergite_tuner's transmons do

    Args:
        device: the quantum device that gate-level jobs compile against
    """
    for name in list(device.edges()):
        device.remove_edge(name)
        _close_instrument(name)
    for name in list(device.elements()):
        device.remove_element(name)
        _close_instrument(name)


def get_frame_frequencies(device: QuantumDevice) -> Dict[str, float]:
    """Gets the calibrated frequency of each frame the device publishes: each transmon's
    drive and readout, and each coupler edge's flux, at its CZ's frequency; a frame of no
    calibrated frequency is left out

    Args:
        device: the quantum device that gate-level jobs compile against

    Returns:
        the frequencies in hertz, by the frames' names, e.g. q12_drive, q12_readout and
        q12_q13_coupler
    """
    frequencies = {}
    for name in device.elements():
        element = device.get_element(name)
        frequencies[f"{name}_drive"] = element.clock_freqs.f01()
        frequencies[f"{name}_readout"] = element.clock_freqs.readout()
    for name in device.edges():
        frequencies[f"{name}_coupler"] = device.get_edge(name).cz_frequency()
    return {
        name: float(frequency)
        for name, frequency in frequencies.items()
        if frequency and not math.isnan(frequency)
    }


def get_edge_names(device: QuantumDevice) -> List[str]:
    """Gets the names of the device's edges, each {parent}_{child}

    Args:
        device: the quantum device

    Returns:
        the names of its edges
    """
    return list(device.edges())


def _get_transmon(name: str) -> BasicTransmonElement:
    """Gets the transmon of the name that QCoDeS keeps, or else creates it; an instrument
    of another type by that name, as a recalibration may leave, is closed first"""
    try:
        instrument = Instrument.find_instrument(name)
    except KeyError:
        return BasicTransmonElement(name)
    if isinstance(instrument, BasicTransmonElement):
        return instrument
    instrument.close()
    return BasicTransmonElement(name)


def _set_transmon(
    transmon: BasicTransmonElement,
    qubit: QubitCalibration,
    resonator: Optional[ResonatorCalibration],
) -> None:
    """Sets the transmon's pulses and clocks from its qubit's and resonator's calibration,
    leaving the defaults of what they lack"""
    transmon.reset.duration(RESET_DURATION)
    _set_if_given(transmon.rxy.amp180, qubit.pi_pulse_amplitude)
    _set_if_given(transmon.rxy.motzoi, qubit.pi_pulse_motzoi)
    if qubit.pi_pulse_duration is not None:
        transmon.rxy.duration(_to_timegrid(qubit.pi_pulse_duration.value))
    _set_if_given(transmon.clock_freqs.f01, qubit.frequency)
    if qubit.frequency is not None and qubit.anharmonicity is not None:
        transmon.clock_freqs.f12(qubit.frequency.value + qubit.anharmonicity.value)
    if resonator is None:
        return
    _set_if_given(transmon.clock_freqs.readout, resonator.frequency)
    _set_if_given(transmon.measure.pulse_amp, resonator.pulse_amplitude)
    if resonator.pulse_duration is not None:
        transmon.measure.pulse_duration(_to_timegrid(resonator.pulse_duration.value))
    if resonator.acq_delay is not None:
        transmon.measure.acq_delay(_to_timegrid(resonator.acq_delay.value))
    if resonator.acq_integration_time is not None:
        transmon.measure.integration_time(
            _to_timegrid(resonator.acq_integration_time.value)
        )


def _set_elements(
    device: QuantumDevice,
    transmons: List[BasicTransmonElement],
    edges: List[CouplerEdge],
) -> None:
    """Makes the device hold the transmons and edges alone, in place of any instruments it
    held by their names: QCoDeS holds instruments only weakly, so those the device does not
    hold are lost to the next garbage collection"""
    for name in list(device.edges()):
        device.remove_edge(name)
    for name in list(device.elements()):
        device.remove_element(name)
    for transmon in transmons:
        device.add_element(transmon)
    for edge in edges:
        device.add_edge(edge)


def _get_coupler_qubit_ids(coupler: CouplerCalibration) -> Tuple[int, int]:
    """Gets the numbers in the ids of the coupler's qubits, in the order of its name"""
    parent, child = coupler.full_name.split("_")
    return int(parent.lstrip("q")), int(child.lstrip("q"))


def _get_edge(parent: str, child: str) -> CouplerEdge:
    """Gets the edge between the two transmons that QCoDeS keeps, or else creates it; an
    instrument of another type by its name is closed first"""
    try:
        edge = Instrument.find_instrument(f"{parent}_{child}")
    except KeyError:
        return CouplerEdge(parent, child)
    if isinstance(edge, CouplerEdge):
        return edge
    edge.close()
    return CouplerEdge(parent, child)


def _set_edge(
    edge: CouplerEdge, coupler: CouplerCalibration, is_parent_control: bool
) -> None:
    """Sets the edge's CZ from its coupler's calibration, which times the pulse as a rise
    and a constant part, and names its phase corrections by control and target"""
    rise = _get_value(coupler.cz_pulse_duration_rise)
    constant = _get_value(coupler.cz_pulse_duration_constant)
    edge.cz_amplitude(_get_value(coupler.cz_pulse_amplitude))
    edge.cz_duration(_to_timegrid(rise + constant))
    edge.cz_wait(_to_timegrid(_get_value(coupler.cz_pulse_duration_before)))
    edge.cz_frequency(_get_value(coupler.frequency))

    control_phase = math.degrees(_get_value(coupler.control_rz_lambda))
    target_phase = math.degrees(_get_value(coupler.target_rz_lambda))
    if is_parent_control:
        edge.parent_phase_correction(control_phase)
        edge.child_phase_correction(target_phase)
    else:
        edge.parent_phase_correction(target_phase)
        edge.child_phase_correction(control_phase)


def _is_parent_control(coupler: CouplerCalibration, parent_id: int) -> bool:
    """Whether the coupler's parent qubit, the first of its name, is the control of its CZ,
    as its calibration says, whose phase corrections are by control and target"""
    return int(str(coupler.control_qubit).lstrip("q")) == parent_id


def _set_if_given(
    parameter: ManualParameter, value: Optional[CalibrationValue]
) -> None:
    """Sets the parameter to the calibration value, unless there is none"""
    if value is not None:
        parameter(value.value)


def _get_value(value: Optional[CalibrationValue]) -> float:
    """Gets the calibration value's number, or 0 if there is none"""
    if value is None:
        return 0.0
    return float(value.value)


def _to_timegrid(seconds: float) -> float:
    """Gets the time on the Qblox time grid nearest the given one, in seconds"""
    return round(seconds / QBLOX_TIMEGRID_INTERVAL) * QBLOX_TIMEGRID_INTERVAL


def _close_instrument(name: str) -> None:
    """Closes the instrument QCoDeS keeps by the name, if it keeps one"""
    if Instrument.exist(name):
        Instrument.find_instrument(name).close()
