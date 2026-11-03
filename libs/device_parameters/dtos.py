# This code is part of Tergite
#
# (C) Copyright Pontus Vikstål (2024)
# (C) Copyright Adilet Tuleouv (2024)
# (C) Copyright Stefan Hill (2024)
# (C) Copyright Martin Ahindura (2024)
# (C) Copyright Chalmers Next Labs AB (2026)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
import copy
from contextlib import suppress
from functools import cached_property
from os import PathLike
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Literal,
    Mapping,
    Optional,
    Self,
    Tuple,
    TypeVar,
    Union,
)

import numpy as np
import toml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from utils.datetime import utc_now_str

from .utils import attach_units_many

if TYPE_CHECKING:
    from tergite_tuner import CalibrationResults
    from tergite_tuner.export import CouplerResults

_CalibValueType = TypeVar("_CalibValueType", int, float, str)
_DOWNCONVERT_FREQUENCY = 4.4e9
# the gates a gate-level job's circuits are transpiled to, which every executor compiles;
# cz too on a device with couplers
GATE_LEVEL_BASIS_GATES = ("rz", "sx", "x", "rx", "measure", "reset", "delay")
# what a frame plays on: a qubit's drive or readout line, or a coupler's flux line
FrameKind = Literal["drive", "readout", "coupler"]


class Device(BaseModel):
    """The schema for device information"""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str
    number_of_qubits: int
    last_online: Optional[str] = None
    is_online: bool
    basis_gates: List[str]
    coupling_map: List[Tuple[int, int]]
    coordinates: List[Tuple[int, int]]
    is_simulator: bool
    coupling_dict: Dict[str, Union[str, List[str]]]
    characterized: bool
    open_pulse: bool
    meas_map: List[List[int]]
    description: str = None
    number_of_couplers: int = 0
    number_of_resonators: int = 0
    dt: Optional[float] = None
    dtm: Optional[float] = None
    qubit_ids: List[str] = []
    meas_lo_freq: Optional[List[int]] = None
    qubit_lo_freq: Optional[List[int]] = None
    gates: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    qubit_ids_coupler_map: List[Tuple[Tuple[int, int], int]] = []

    @classmethod
    def from_config(cls, conf: "BackendConfig") -> "Device":
        """Generates a device instance from the backend config

        Args:
            conf: the backend configuration from which to extract the device info

        Returns:
            an instance of Device extracted from the backend config
        """
        qubit_ids = conf.device_config.qubit_ids
        return cls(
            **conf.general_config.model_dump(),
            meas_map=conf.device_config.meas_map,
            qubit_ids=qubit_ids,
            gates=conf.gates,
            number_of_qubits=conf.general_config.num_qubits,
            number_of_couplers=conf.general_config.num_couplers,
            number_of_resonators=conf.general_config.num_resonators,
            last_online=conf.general_config.online_date,
            is_online=conf.general_config.is_active,
            basis_gates=[
                *GATE_LEVEL_BASIS_GATES,
                *(["cz"] if conf.device_config.coupling_dict else []),
            ],
            coupling_map=conf.device_config.coupling_map,
            coordinates=conf.device_config.coordinates,
            is_simulator=conf.general_config.simulator,
            coupling_dict=conf.device_config.coupling_dict,
            qubit_ids_coupler_map=conf.device_config.qubit_ids_coupler_map,
        )


class CalibrationValue(BaseModel, Generic[_CalibValueType]):
    """A calibration value"""

    model_config = ConfigDict(extra="allow")

    value: _CalibValueType
    unit: str = ""
    date: Optional[str] = None

    @classmethod
    def from_scalar(
        cls, value: _CalibValueType | None, unit: str = "", **kwargs
    ) -> Optional[Self]:
        """Converts a scalar to a calibration value if not None

        Args:
            value: the scalar to convert
            unit: the unit
            **kwargs: other arguments

        Returns:
            the calibration value or None if value is None
        """
        if value is None:
            return None
        return cls(value=value, unit=unit, **kwargs)


class QubitCalibration(BaseModel):
    """Schema for the calibration data of the qubit"""

    model_config = ConfigDict(extra="allow")

    t1_decoherence: Optional[CalibrationValue[float]] = None
    t2_decoherence: Optional[CalibrationValue[float]] = None
    frequency: Optional[CalibrationValue[float]] = None
    anharmonicity: Optional[CalibrationValue[float]] = None
    readout_assignment_error: Optional[CalibrationValue[float]] = None
    # parameters for x gate
    pi_pulse_amplitude: Optional[CalibrationValue[float]] = None
    pi_pulse_duration: Optional[CalibrationValue[float]] = None
    pi_pulse_motzoi: Optional[CalibrationValue[float]] = None
    pulse_type: Optional[CalibrationValue[str]] = None
    pulse_sigma: Optional[CalibrationValue[float]] = CalibrationValue.from_scalar(0)
    id: Optional[int] = None
    index: Optional[CalibrationValue[int]] = None
    x_position: Optional[CalibrationValue[int]] = None
    y_position: Optional[CalibrationValue[int]] = None
    xy_drive_line: Optional[CalibrationValue[int]] = None
    z_drive_line: Optional[CalibrationValue[int]] = None

    @field_validator("id", mode="before")
    @classmethod
    def prep_id(cls, v):
        """Accepts the id as a number or as a name like "q1"

        Args:
            v: the raw id

        Returns:
            the id as a number
        """
        if isinstance(v, str):
            return int(v.lstrip("q"))
        return int(v)

    @computed_field
    @cached_property
    def name(self) -> Optional[str]:
        """The name like "q1", or None if there is no id"""
        if self.id is None:
            return None
        return f"q{self.id}"

    @classmethod
    def from_calib_config(
        cls, conf: "_BackendCalibrationConfig"
    ) -> List["QubitCalibration"]:
        """Generates new instances of QubitCalibration from the backend calibration config

        Args:
            conf: the backend calibration config

        Returns:
            the list of new instance of QubitCalibration
        """
        qubit_units = conf.units.get("qubit", {})
        qubit_data = attach_units_many(conf.qubit, qubit_units)

        results: List[QubitCalibration] = []
        for qubit_conf in qubit_data:
            qubit_conf["id"] = qubit_conf["id"]["value"]
            results.append(QubitCalibration.model_validate(qubit_conf))

        return results

    @classmethod
    def from_calib_results(
        cls, calib_results: "CalibrationResults", **kwargs
    ) -> List["QubitCalibration"]:
        """Generates new instances of QubitCalibration from the calibration results

        Args:
            calib_results: tergite-tuner's results of a recalibration

        Returns:
            the list of new instances of QubitCalibration
        """
        results: List[QubitCalibration] = []
        for qubit, qubit_data in calib_results.transmons.items():
            results.append(
                QubitCalibration(
                    id=qubit,
                    frequency=CalibrationValue.from_scalar(
                        qubit_data.clock_freqs.f01, unit="Hz"
                    ),
                    pi_pulse_amplitude=CalibrationValue.from_scalar(
                        qubit_data.rxy.amp180
                    ),
                    pi_pulse_duration=CalibrationValue.from_scalar(
                        qubit_data.rxy.duration, unit="s"
                    ),
                    pi_pulse_motzoi=CalibrationValue.from_scalar(qubit_data.rxy.motzoi),
                    pulse_type=CalibrationValue.from_scalar("Gaussian"),
                    pulse_sigma=CalibrationValue.from_scalar(qubit_data.rxy.sigma),
                    t1_decoherence=CalibrationValue.from_scalar(
                        qubit_data.t1_time, unit="us"
                    ),
                    t2_decoherence=CalibrationValue.from_scalar(
                        qubit_data.t2_echo_time, unit="us"
                    ),
                )
            )

        return results


class ResonatorCalibration(BaseModel):
    """Schema for the calibration data of the resonator"""

    model_config = ConfigDict(extra="allow")

    acq_delay: Optional[CalibrationValue[float]] = None
    acq_integration_time: Optional[CalibrationValue[float]] = None
    frequency: Optional[CalibrationValue[float]] = None
    pulse_amplitude: Optional[CalibrationValue[float]] = None
    pulse_delay: Optional[CalibrationValue[float]] = None
    pulse_duration: Optional[CalibrationValue[float]] = None
    pulse_type: Optional[CalibrationValue[str]] = None
    id: Optional[int] = None
    index: Optional[CalibrationValue[int]] = None
    x_position: Optional[CalibrationValue[int]] = None
    y_position: Optional[CalibrationValue[int]] = None
    readout_line: Optional[CalibrationValue[int]] = None

    @field_validator("id", mode="before")
    @classmethod
    def prep_id(cls, v):
        """Accepts the id as a number or as a name like "q1"

        Args:
            v: the raw id

        Returns:
            the id as a number
        """
        if isinstance(v, str):
            return int(v.lstrip("q"))
        return int(v)

    @computed_field
    @cached_property
    def name(self) -> Optional[str]:
        """The name like "q1", or None if there is no id"""
        if self.id is None:
            return None
        return f"q{self.id}"

    @classmethod
    def from_calib_config(
        cls, conf: "_BackendCalibrationConfig"
    ) -> List["ResonatorCalibration"]:
        """Generates new instances of ResonatorCalibration from the backend calibration config

        Args:
            conf: the backend calibration config

        Returns:
            the list of new instance of ResonatorCalibration
        """
        resonator_units = conf.units.get("readout_resonator", {})
        resonator_data = attach_units_many(conf.readout_resonator, resonator_units)

        results: List[ResonatorCalibration] = []
        for resonator_conf in resonator_data:
            resonator_conf["id"] = resonator_conf["id"]["value"]
            results.append(ResonatorCalibration.model_validate(resonator_conf))

        return results

    @classmethod
    def from_calib_results(
        cls, calib_results: "CalibrationResults", **kwargs
    ) -> List["ResonatorCalibration"]:
        """Generates new instances of ResonatorCalibration from the calibration results

        Args:
            calib_results: the calibration results

        Returns:
            the list of new instances of ResonatorCalibration
        """
        results: List[ResonatorCalibration] = []
        for qubit, item in calib_results.transmons.items():
            results.append(
                ResonatorCalibration(
                    id=qubit,
                    acq_delay=CalibrationValue.from_scalar(
                        item.measure.acq_delay, unit="s"
                    ),
                    acq_integration_time=CalibrationValue.from_scalar(
                        item.measure.integration_time, unit="s"
                    ),
                    frequency=CalibrationValue.from_scalar(
                        item.extended_clock_freqs.readout_2state_opt, unit="Hz"
                    ),
                    pulse_delay=CalibrationValue.from_scalar(
                        item.measure.ro_pulse_delay, unit="s"
                    ),
                    pulse_duration=CalibrationValue.from_scalar(
                        item.measure.pulse_duration, unit="s"
                    ),
                    pulse_amplitude=CalibrationValue.from_scalar(
                        item.measure_2state_opt.pulse_amp
                    ),
                    pulse_type=CalibrationValue.from_scalar("Square"),
                )
            )
        return results


class CouplerCalibration(BaseModel):
    """Schema for the calibration data of the coupler"""

    model_config = ConfigDict(extra="allow")

    frequency: Optional[CalibrationValue[float]] = None
    frequency_detuning: Optional[CalibrationValue[float]] = None
    anharmonicity: Optional[CalibrationValue[float]] = None
    coupling_strength_02: Optional[CalibrationValue[float]] = None
    coupling_strength_12: Optional[CalibrationValue[float]] = None
    cz_pulse_amplitude: Optional[CalibrationValue[float]] = None
    cz_pulse_dc_bias: Optional[CalibrationValue[float]] = None
    cz_pulse_phase_offset: Optional[CalibrationValue[float]] = None
    cz_pulse_duration_before: Optional[CalibrationValue[float]] = None
    cz_pulse_duration_rise: Optional[CalibrationValue[float]] = None
    cz_pulse_duration_constant: Optional[CalibrationValue[float]] = None
    control_rz_lambda: Optional[CalibrationValue] = None
    target_rz_lambda: Optional[CalibrationValue] = None
    pulse_type: Optional[CalibrationValue[str]] = None
    id: Optional[int] = None
    full_name: Optional[str] = None
    """The name of the coupler in format {control_qubit}_{target_qubit}"""
    # FIXME: This format is not being followed currently

    @field_validator("id", mode="before")
    @classmethod
    def prep_id(cls, v):
        """Accepts the id as a number or as a name like "u1"

        Args:
            v: the raw id

        Returns:
            the id as a number
        """
        if isinstance(v, str):
            return int(v.lstrip("u"))
        return v

    @field_validator("full_name", mode="before")
    @classmethod
    def validate_full_name(cls, v):
        """Accepts the full name as is or as a calibration value

        Args:
            v: the raw full name

        Returns:
            the full name
        """
        if isinstance(v, dict):
            return v.get("value")
        return v

    @computed_field
    @cached_property
    def short_name(self) -> Optional[str]:
        """The name like "u1", or None if there is no id"""
        if self.id is None:
            return None
        return f"u{self.id}"

    @computed_field
    @cached_property
    def control_qubit(self) -> Optional[str]:
        """The name of the control qubit, or None if there is no full name"""
        # FIXME: the format '{control}_{target}' is not being used at the moment
        if self.full_name is None:
            return None
        return self.full_name.split("_")[0]

    @computed_field
    @cached_property
    def target_qubit(self) -> Optional[str]:
        """The name of the target qubit, or None if there is no full name"""
        # FIXME: the format '{control}_{target}' is not being used at the moment
        if self.full_name is None:
            return None
        return self.full_name.split("_")[1]

    @classmethod
    def from_calib_config(
        cls,
        conf: "_BackendCalibrationConfig",
        coupling_dict: Dict[str, Tuple[str, str]],
    ) -> List["CouplerCalibration"]:
        """Generates new instances of CouplerCalibration from the backend calibration config,
        each named for its qubits in the device config's coupling_dict unless the seed names it

        Args:
            conf: the backend calibration config
            coupling_dict: the device config's map of each coupler to its qubits

        Returns:
            the list of new instance of CouplerCalibration

        Raises:
            ValueError: coupler {name} is in neither the seed's names nor the coupling_dict
        """
        coupler_units = conf.units.get("coupler", {})
        coupler_data = attach_units_many(conf.coupler, coupler_units)

        results: List[CouplerCalibration] = []
        for coupler_conf in coupler_data:
            coupler_conf["id"] = coupler_conf["id"]["value"]
            if not coupler_conf.get("full_name"):
                coupler_conf["full_name"] = _get_coupler_name(
                    coupler_conf["id"], coupling_dict
                )
            results.append(CouplerCalibration.model_validate(coupler_conf))

        return results

    @classmethod
    def from_calib_results(
        cls,
        calib_results: "CalibrationResults",
        name_id_map: Dict[str, int],
        reverse_phase_qubits: Tuple[str, ...] = (),
        **kwargs,
    ) -> List["CouplerCalibration"]:
        """Generates new instances of CouplerCalibration from the calibration results

        Args:
            calib_results: the calibration results
            name_id_map: the map for the names of couplers
                    in calibration to their index in BCC
            reverse_phase_qubits: the qubits that have the reverse phase of what
                they should have phase on the couplers
            kwargs: extra args like:
                coupler_index_map (dict[str, int]): the required map for the names of couplers
                    in calibration to their index in BCC

        Returns:
            the list of new instances of CouplerCalibration
        """
        results: List[CouplerCalibration] = []
        for coupler, item in calib_results.couplers.items():
            try:
                bcc_coupler_idx = name_id_map[coupler]
            except KeyError:
                # ignore couplers missing in the map
                continue

            control_phase, target_phase = _get_local_phases(
                coupler, item, reverse_phase_qubits
            )

            results.append(
                CouplerCalibration(
                    id=bcc_coupler_idx,
                    full_name=coupler,
                    # correcting the frequency by the downconvert frequency
                    frequency=CalibrationValue.from_scalar(
                        value=_DOWNCONVERT_FREQUENCY - item.cz_pulse_frequency,
                        unit="Hz",
                    ),
                    cz_pulse_amplitude=CalibrationValue.from_scalar(
                        item.cz_pulse_amplitude
                    ),
                    cz_pulse_dc_bias=CalibrationValue.from_scalar(item.parking_current),
                    cz_pulse_duration_constant=CalibrationValue.from_scalar(
                        item.cz_pulse_duration, unit="s"
                    ),
                    control_rz_lambda=CalibrationValue.from_scalar(
                        control_phase, unit="rad"
                    ),
                    target_rz_lambda=CalibrationValue.from_scalar(
                        target_phase, unit="rad"
                    ),
                    pulse_type=CalibrationValue.from_scalar("wacqt_cz"),
                )
            )

        return results


class PulseFrame(BaseModel):
    """A frame a pulse-level job may play on, at its calibrated frequency"""

    # as a job names it, e.g. q12_drive or q12_q13_coupler
    name: str
    kind: FrameKind
    # the physical qubits it acts on, as a job numbers them, e.g. 0 for $0
    qubits: List[int]
    # in hertz
    frequency: float


class GateDefcal(BaseModel):
    """A gate's calibrated pulses as an OpenPulse defcal, which a job may override"""

    # e.g. x, sx, rz or cz
    gate: str
    # the physical qubits it is calibrated on, as a job numbers them
    qubits: List[int]
    # the defcal, e.g. 'defcal x $0 { ... }'
    program: str


class DeviceCalibration(BaseModel):
    """Schema for the calibration data of a given device"""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str
    qubits: List[QubitCalibration]
    resonators: Optional[List[ResonatorCalibration]] = None
    couplers: Optional[List[CouplerCalibration]] = None
    discriminators: Optional[Dict[str, Any]] = None
    # those of a device that takes pulse-level jobs; none of one that does not
    frames: Optional[List[PulseFrame]] = None
    defcals: Optional[List[GateDefcal]] = None
    last_calibrated: str

    def to_calibration_config(self) -> "_BackendCalibrationConfig":
        """Gets the calibration config of a calibration seed file, with each value's unit

        Returns:
            the calibration config of this calibration data
        """
        units = _to_units(self)
        scalars = _to_scalars(self)

        qubit_units = {}
        coupler_units = {}
        resonator_units = {}

        with suppress(KeyError, IndexError):
            qubit_units = units["qubits"][0]
            qubit_units.pop("id")
        with suppress(KeyError, IndexError, TypeError):
            coupler_units = units["couplers"][0]
            coupler_units.pop("id")
        with suppress(KeyError, IndexError, TypeError):
            resonator_units = units["resonators"][0]
            resonator_units.pop("id")
        return _BackendCalibrationConfig(
            units={
                "qubit": qubit_units,
                "coupler": coupler_units,
                "readout_resonator": resonator_units,
                "discriminators": {},
            },
            qubit=scalars["qubits"],
            coupler=scalars["couplers"],
            readout_resonator=scalars["resonators"],
            discriminators=self.discriminators,
        )

    @classmethod
    def from_config(cls, conf: "BackendConfig") -> "DeviceCalibration":
        """Generates a device calibration instance from the backend config

        Args:
            conf: the backend configuration from which to extract the calibration info

        Returns:
            an instance of DeviceCalibration go from the backend config

        Raises:
            ValueError: calibration_config is None on backend config
        """
        calib_config = conf.calibration_config
        if calib_config is None:
            raise ValueError("calibration_config is None on backend config")

        return cls(
            name=conf.general_config.name,
            version=conf.general_config.version,
            qubits=QubitCalibration.from_calib_config(calib_config),
            resonators=ResonatorCalibration.from_calib_config(calib_config),
            couplers=CouplerCalibration.from_calib_config(
                calib_config, coupling_dict=conf.device_config.coupling_dict
            ),
            discriminators=calib_config.discriminators,
            last_calibrated=utc_now_str(),
        )

    @classmethod
    def from_calib_results(
        cls,
        calib_results: "CalibrationResults",
        name_id_map: Dict[str, int],
        reverse_phase_qubits: Tuple[str, ...] = (),
        **kwargs: Any,
    ) -> "DeviceCalibration":
        """Generates a new instance of DeviceCalibration from the calibration results

        Args:
            calib_results: the calibration results
            name_id_map: the map for the names of couplers/qubits
                    in calibration calib_results to their identifier in BCC
            reverse_phase_qubits: the qubits that have their phases reversed on couplers
            kwargs: extra args like:

            kwargs: extra args to add to the DeviceCalibration

        Returns:
            the new instance of DeviceCalibration
        """
        kwargs.setdefault("last_calibrated", utc_now_str())
        kwargs.setdefault("version", "0.0.0")

        return DeviceCalibration(
            **kwargs,
            qubits=QubitCalibration.from_calib_results(calib_results),
            resonators=ResonatorCalibration.from_calib_results(calib_results),
            couplers=CouplerCalibration.from_calib_results(
                calib_results,
                name_id_map=name_id_map,
                reverse_phase_qubits=reverse_phase_qubits,
            ),
            discriminators={
                "lda": {
                    qubit: {
                        "coef_0": item.lda_coef_0,
                        "coef_1": item.lda_coef_1,
                        "intercept": item.lda_intercept,
                    }
                    for qubit, item in calib_results.transmons.items()
                }
            },
        )


class _BackendGeneralConfig(BaseModel):
    """The basic config of the backend"""

    name: str
    num_qubits: int
    num_couplers: int
    num_resonators: int
    dt: float
    dtm: float
    description: str = ""
    is_active: bool = True
    characterized: bool = True
    open_pulse: bool = True
    simulator: bool = False
    version: str = "0.0.0"
    online_date: str = Field(default_factory=utc_now_str)


class _BackendDeviceConfig(BaseModel):
    """The device config for the backend"""

    qubit_ids: List[str]
    discriminators: List[str] = ["lda", "thresholded_acquisition"]
    # unidirectional map of coupler to qubit pair e.g. {u1: (q1,q2), u3: (q2,q3)}
    coupling_dict: Dict[str, Tuple[str, str]] = {}
    # the [x, y] coordinates of the qubits
    coordinates: List[Tuple[int, int]] = []

    meas_map: List[List[int]] = []
    qubit_parameters: List[str] = []
    resonator_parameters: List[str] = []

    @field_validator("qubit_ids")
    @classmethod
    def check_qubit_order(cls, qubit_ids: List[str]) -> List[str]:
        """Requires the qubits in the order of their numbers: a gate-level circuit's
        qubit i is the i-th by number, and the coupling map sent to MSS indexes qubits
        by their place in this list, so the two must agree

        Raises:
            ValueError: qubit_ids {ids} are not in the order of their numbers
        """
        numbers = [_get_number(qubit_id) for qubit_id in qubit_ids]
        if numbers != sorted(numbers):
            raise ValueError(
                f"qubit_ids {qubit_ids} are not in the order of their numbers"
            )
        return qubit_ids

    coupler_parameters: List[str] = []
    discriminator_parameters: Dict[str, List[str]] = {}
    fixed_duration_couplers: Tuple[str, ...] = ()
    reverse_phase_qubits: Tuple[str, ...] = ()

    @property
    def qubit_numbers(self) -> List[int]:
        """The numbers in the qubits' ids, in order, e.g. [12, 13] of q13 and q12: the
        qubit of index i in a job's circuit is the i-th"""
        return sorted(_get_number(qubit_id) for qubit_id in self.qubit_ids)

    @computed_field
    @property
    def coupling_map(self) -> List[Tuple[int, int]]:
        """The bi-directional couplings, each qubit by its index in qubit_ids, e.g. 1-to-2 as
        (1, 2) and (2, 1); or, with no couplers, each qubit coupled with itself"""
        if len(self.coupling_dict) == 0:
            return [(idx, idx) for idx, _ in enumerate(self.qubit_ids)]

        qubit_index = {_id: psn for psn, _id in enumerate(self.qubit_ids)}
        index_couplings = [
            (qubit_index[q1], qubit_index[q2]) for q1, q2 in self.coupling_dict.values()
        ]
        return index_couplings + [(q2, q1) for q1, q2 in index_couplings]

    @computed_field
    @property
    def qubit_ids_coupler_map(self) -> List[Tuple[Tuple[int, int], int]]:
        """The bi-directional couplings, each with its coupler, the qubits and couplers by
        the numbers in their ids, e.g. [((1, 2), 1), ((2, 1), 1)]"""
        id_coupling_items = [
            ((_get_number(q1), _get_number(q2)), _get_number(c))
            for c, (q1, q2) in self.coupling_dict.items()
        ]
        return id_coupling_items + [((q2, q1), c) for (q1, q2), c in id_coupling_items]


class _BackendCalibrationConfig(BaseModel):
    """The device config for the simulated or dummy backends"""

    # Adjusted the type hint for units to support nested structure within discriminators
    units: Dict[
        Literal["qubit", "readout_resonator", "discriminators", "coupler"],
        Dict[str, str],
    ] = {}
    qubit: List[Dict[str, Union[float, str]]] = []
    readout_resonator: List[Dict[str, Union[float, str]]] = []
    discriminators: Dict[str, Dict[str, Dict[str, Union[float, str]]]] = {}
    coupler: List[Dict[str, Union[float, str]]] = []


_NonNegative = Annotated[float, Field(ge=0)]
_Positive = Annotated[float, Field(gt=0)]


class PulseLimits(BaseModel):
    """What a device's pulse-level jobs may play, which its operators set in the backend
    config's pulse_limits: amplitudes as fractions of full scale, energies in full scale
    squared times seconds, on one frame. A kind of frame a limit by kind leaves out may not
    be played"""

    model_config = ConfigDict(extra="forbid")

    max_amplitude: Dict[FrameKind, _NonNegative]
    max_frequency_offset: _NonNegative
    max_energy_per_shot: Dict[FrameKind, _NonNegative]
    max_energy_per_job: Dict[FrameKind, _NonNegative]
    max_pulse_duration: _NonNegative
    max_samples_per_waveform: Annotated[int, Field(ge=0)]
    max_capture_duration: _NonNegative


class ChipQubit(BaseModel):
    """A simulated chip's transmon, as it is, not as its calibration finds it"""

    model_config = ConfigDict(extra="forbid")

    # of the transition from |0> to |1>, in hertz
    frequency: _Positive
    # the frequency of |1> to |2> less that of |0> to |1>, in hertz; negative for a transmon
    anharmonicity: float
    # the Rabi frequency, in hertz, of a resonant drive of amplitude 1
    rabi_frequency: _Positive
    t1: _Positive
    t2: _Positive

    @model_validator(mode="after")
    def check_t2(self) -> Self:
        """Requires a T2 no longer than twice the T1

        Raises:
            ValueError: t2 {t2} is longer than twice t1 {t1}
        """
        if self.t2 > 2 * self.t1:
            raise ValueError(f"t2 {self.t2} is longer than twice t1 {self.t1}")
        return self


class ChipResonator(BaseModel):
    """A simulated chip's readout resonator, a hanger on the feedline whose dip moves with
    its transmon's level, and towards its bare frequency as the readout grows stronger
    """

    model_config = ConfigDict(extra="forbid")

    # of its dip with its transmon in |0>, at a weak readout, in hertz
    frequency: _Positive
    # the shift of its dip, in hertz, with its transmon in |1>; twice that in |2>
    dispersive_shift: float
    # of its dip at a readout too strong for the transmon to shift it, in hertz
    bare_frequency: _Positive
    # the readout amplitude at which its dip is half way to its bare frequency
    critical_amplitude: _Positive
    internal_quality: _Positive
    coupling_quality: _Positive
    # the transmission off resonance of a readout of amplitude 1, and its phase in radians
    amplitude: _Positive = 1.0
    phase: float = 0.0


class ChipCoupler(BaseModel):
    """A simulated chip's coupler, a flux-tunable transmon between two qubits"""

    model_config = ConfigDict(extra="forbid")

    # of its transition from |0> to |1> at zero flux, in hertz
    max_frequency: _Positive
    anharmonicity: float
    # its coupling to each of its qubits, by qubit id, in hertz
    couplings: Dict[str, _Positive]
    # its flux, in flux quanta, without a current or a pulse
    flux_offset: float = 0.0
    # the flux, in flux quanta, of an ampere of DC current, and of a flux pulse of amplitude 1
    flux_per_current: float = 0.0
    flux_per_amplitude: float

    @field_validator("couplings")
    @classmethod
    def check_two_qubits(cls, couplings: Dict[str, float]) -> Dict[str, float]:
        """Requires a coupling to each of two qubits

        Raises:
            ValueError: a coupler couples two qubits, not {qubits}
        """
        if len(couplings) != 2:
            raise ValueError(f"a coupler couples two qubits, not {list(couplings)}")
        return couplings


class ChipConfig(BaseModel):
    """The true parameters of a simulated chip, which its calibration does not see, by the
    ids of its qubits and couplers: a resonator by its qubit's id, a coupler by its
    qubits', e.g. q12_q13"""

    model_config = ConfigDict(extra="forbid")

    qubits: Dict[str, ChipQubit]
    resonators: Dict[str, ChipResonator]
    couplers: Dict[str, ChipCoupler] = {}
    # the standard deviation of a shot's I, and of its Q, integrated over a microsecond
    readout_noise: _NonNegative

    @model_validator(mode="after")
    def check_elements(self) -> Self:
        """Requires a resonator for each qubit, and the qubits of each coupler

        Raises:
            ValueError: the qubits {ids} have no resonator
            ValueError: the coupler {id} couples qubits {ids} the chip lacks
        """
        unread = sorted(set(self.qubits) - set(self.resonators))
        if unread:
            raise ValueError(f"the qubits {unread} have no resonator")
        for name, coupler in self.couplers.items():
            missing = sorted(set(coupler.couplings) - set(self.qubits))
            if missing:
                raise ValueError(
                    f"the coupler {name} couples qubits {missing} the chip lacks"
                )
        return self


class SimulatorConfig(BaseModel):
    """A simulated device, which its backend config's simulator section sets: its chip,
    on which its schedules play as compiled for its dummy clusters; or, without one, how
    QuTiP reads its qubits out, its jobs played on a model drawn from its calibration"""

    model_config = ConfigDict(extra="forbid")

    # each qubit's IQ point of state 0 and of state 1, by its id, e.g. {"q0": [[1, 0], [-1, 0]]}
    readout_iq: Optional[Dict[str, Tuple[Tuple[float, float], Tuple[float, float]]]] = (
        None
    )
    # the standard deviation of a shot's I, and of its Q, about its state's point
    readout_noise: Optional[_NonNegative] = None
    chip: Optional[ChipConfig] = None

    @model_validator(mode="after")
    def check_readout(self) -> Self:
        """Requires a chip, or the readout's IQ points and noise

        Raises:
            ValueError: a simulator section takes a chip, or readout_iq and readout_noise
        """
        if self.chip is None and (
            self.readout_iq is None or self.readout_noise is None
        ):
            raise ValueError(
                "a simulator section takes a chip, or readout_iq and readout_noise"
            )
        return self


class BackendConfig(BaseModel):
    """The configration as read from the file"""

    name: Optional[str] = None
    general_config: _BackendGeneralConfig
    device_config: _BackendDeviceConfig
    gates: Dict[str, Dict[str, Any]] = {}
    calibration_config: Optional[_BackendCalibrationConfig] = None
    # none takes no pulse-level jobs
    pulse_limits: Optional[PulseLimits] = None
    # a simulated device's; none of one on clusters
    simulator: Optional[SimulatorConfig] = None

    @classmethod
    def from_toml(cls, file: PathLike, seed_file: PathLike):
        """Creates a BackendConfig instance from a TOML file

        Args:
            file: the path to the backend config TOML file
            seed_file: the path to the calibration seed TOML file; its calibration
                config is left out if the file or its section is missing

        Returns:
            the new BackendConfig
        """
        config = toml.load(file)

        calibration_config = None
        try:
            seed_data = toml.load(seed_file)
            calibration_config = _BackendCalibrationConfig(
                **seed_data["calibration_config"]
            )
        except (FileNotFoundError, KeyError):
            pass

        return cls(
            general_config=_BackendGeneralConfig(**config.get("general_config", {})),
            device_config=_BackendDeviceConfig(**config.get("device_config", {})),
            gates={k: dict(v) for k, v in config.get("gates", {}).items()},
            calibration_config=calibration_config,
            pulse_limits=config.get("pulse_limits"),
            simulator=config.get("simulator"),
        )

    @model_validator(mode="after")
    def check_simulator_config(self):
        """Requires a calibration config for a simulator

        Raises:
            ValueError: Calibration config is required for simulators.
        """
        if self.general_config.simulator and self.calibration_config is None:
            raise ValueError("Calibration config is required for simulators.")

        return self

    @model_validator(mode="after")
    def set_name(self):
        """Sets the name basing on the general config"""
        self.name = self.general_config.name
        return self


def _get_local_phases(
    coupler: str,
    coupler_results: "CouplerResults",
    reverse_phase_qubits: Tuple[str, ...],
) -> Tuple[float, float]:
    """Gets the local phases in radians for given the coupler_results from tergite-tuner

    Args:
        coupler: the coupler name
        coupler_results: the coupler's results from tergite-tuner
        reverse_phase_qubits: the qubits that have the reverse phase of what they sh
            should have phase on their couplers

    Returns:
        tuple of (control_qubit_local_phase, target_qubit_local_phase)
    """
    control_phase = coupler_results.cz_dynamic_control
    target_phase = coupler_results.cz_dynamic_target

    q1, q2 = coupler.split("_")
    affected_qubits = set(reverse_phase_qubits) & {q1, q2}

    # flip phases of the affected qubits
    for q in affected_qubits:
        # FIXME: Major assumption: the even qubit is the control one,
        #  and the odd is target
        idx = int(q.lstrip("q"))
        is_even = idx % 2 == 0
        if is_even:
            control_phase = -control_phase
        else:
            target_phase = -target_phase

    return np.deg2rad(control_phase), np.deg2rad(target_phase)


def _to_units(calibration: BaseModel) -> Dict[str, Any]:
    """Gets the calibration as a dict of the units of its values, leaving out the None"""
    return _map_calibration_values(calibration, lambda value: value.unit, False)


def _to_scalars(calibration: BaseModel) -> Dict[str, Any]:
    """Gets the calibration as a dict of its values' scalars and its computed fields, leaving
    out the None"""
    return _map_calibration_values(calibration, lambda value: value.value, True)


def _map_calibration_values(
    item: Any,
    get_leaf: Callable[[CalibrationValue], Any],
    should_include_computed: bool,
) -> Any:
    """Copies the item, with each calibration value in it, however deep, replaced by what
    get_leaf gets of it, and each other model by a dict of its fields but the None
    """
    if isinstance(item, CalibrationValue):
        return get_leaf(item)
    if isinstance(item, BaseModel):
        fields = {
            name: _map_calibration_values(
                getattr(item, name), get_leaf, should_include_computed
            )
            for name in type(item).model_fields
            if getattr(item, name) is not None
        }
        if should_include_computed:
            fields.update(
                {name: getattr(item, name) for name in type(item).model_computed_fields}
            )
        return fields
    if isinstance(item, (list, tuple)):
        return [
            _map_calibration_values(value, get_leaf, should_include_computed)
            for value in item
        ]
    if isinstance(item, Mapping):
        return {
            key: _map_calibration_values(value, get_leaf, should_include_computed)
            for key, value in item.items()
        }
    return copy.copy(item)


def _get_number(_id: str) -> int:
    """Gets the number in a qubit's or coupler's id, e.g. 2 of q2"""
    return int(_id.strip("q").strip("u"))


def _get_coupler_name(
    coupler_id: Union[str, int], coupling_dict: Dict[str, Tuple[str, str]]
) -> str:
    """Gets the coupler's name, {control_qubit}_{target_qubit}, from the coupling_dict

    Raises:
        ValueError: coupler {name} is in neither the seed's names nor the coupling_dict
    """
    short_name = f"u{int(str(coupler_id).lstrip('u'))}"
    if short_name not in coupling_dict:
        raise ValueError(
            f"coupler {short_name} is in neither the seed's names nor the device config's"
            " coupling_dict"
        )
    return "_".join(coupling_dict[short_name])
