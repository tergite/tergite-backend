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
from abc import ABC, abstractmethod
from contextlib import suppress
from os import PathLike
from typing import (
    Any,
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
from pydantic import BaseModel, ConfigDict, model_validator

from app.utils.compat import CalibrationResults, _CouplerInRedis
from app.utils.datetime import utc_now_str
from app.utils.redis_store import Schema

from .utils import attach_units_many

_CalibValueType = TypeVar("_CalibValueType", int, float, str)
_DOWNCONVERT_FREQUENCY = 4.4e9


class Device(Schema):
    """The schema for device information"""

    __primary_key_fields__ = ("name",)
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
            basis_gates=list(conf.gates.keys()),
            coupling_map=conf.device_config.coupling_map,
            coordinates=conf.device_config.coordinates,
            is_simulator=conf.general_config.simulator,
            coupling_dict=conf.device_config.coupling_dict,
            qubit_ids_coupler_map=conf.device_config.qubit_ids_coupler_map,
        )


class CalibrationModel(BaseModel, ABC):
    """The base class of calibration model"""

    model_config = ConfigDict(extra="allow")

    @abstractmethod
    def model_dump_scalar(self, **kwargs) -> Any:
        """Returns the calibration value as a scalar"""
        ...

    @abstractmethod
    def model_dump_unit(self, **kwargs) -> Any:
        """Returns the unit of the value"""
        ...


class CalibrationValue(CalibrationModel, Generic[_CalibValueType]):
    """A calibration value"""

    value: _CalibValueType
    unit: str = ""
    date: Optional[str] = None

    def model_dump_scalar(self, **kwargs) -> _CalibValueType:
        """Returns the calibration value as a scalar"""
        return self.value

    def model_dump_unit(self, **kwargs) -> str:
        """Returns the unit of the value"""
        return self.unit

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


class CalibrationValueSet(CalibrationModel, ABC):
    """A collection of calibration values"""

    model_config = ConfigDict(extra="allow")

    @classmethod
    @abstractmethod
    def from_calib_results(
        cls, data: CalibrationResults, name_id_map: Dict[str, Any], **kwargs: Any
    ) -> List[Self]:
        """Generates new instances of this calibration value set from the calibration results

        Args:
            data: the for the qubit in redis
            name_id_map: the map of the names in the data to the identifier used in BCC

        Returns:
            the list of new instances of calibration value sets
        """
        ...

    def model_dump_unit(
        self,
        exclude_none: bool = False,
        **kwargs,
    ) -> Dict[str, _CalibValueType]:
        """Generates a dict with all the calibration values' units

        Args:
            exclude_none: whether to exclude none values or not

        Returns:
            A dictionary representation of the units of the model.
        """
        data = {}
        fields = self.__class__.model_fields
        for k in fields.keys():
            v = copy.copy(getattr(self, k))
            if v is None and exclude_none:
                continue

            data[k] = v
            if isinstance(v, CalibrationModel):
                data[k] = v.model_dump_unit(exclude_none=exclude_none, **kwargs)
            elif isinstance(v, (list, tuple)):
                for idx, item in enumerate(v):
                    if isinstance(item, CalibrationModel):
                        data[k][idx] = item.model_dump_unit(
                            exclude_none=exclude_none, **kwargs
                        )
            elif isinstance(v, Mapping):
                for key, item in v.items():
                    if isinstance(item, CalibrationModel):
                        data[k][key] = item.model_dump_unit(
                            exclude_none=exclude_none, **kwargs
                        )
        return data

    def model_dump_scalar(
        self,
        exclude_none: bool = False,
        **kwargs,
    ) -> Dict[str, _CalibValueType]:
        """Generates a dict with all the calibration values converted to scalars

        Args:
            exclude_none: whether to exclude none values or not

        Returns:
            A dictionary representation of the model.
        """
        data = {}
        fields = self.__class__.model_fields
        for k in fields.keys():
            v = copy.copy(getattr(self, k))
            if v is None and exclude_none:
                continue

            data[k] = v
            if isinstance(v, CalibrationModel):
                data[k] = v.model_dump_scalar(exclude_none=exclude_none, **kwargs)
            elif isinstance(v, (list, tuple)):
                for idx, item in enumerate(v):
                    if isinstance(item, CalibrationModel):
                        data[k][idx] = item.model_dump_scalar(
                            exclude_none=exclude_none, **kwargs
                        )
            elif isinstance(v, Mapping):
                for key, item in v.items():
                    if isinstance(item, CalibrationModel):
                        data[k][key] = item.model_dump_scalar(
                            exclude_none=exclude_none, **kwargs
                        )
        return data


class QubitCalibration(CalibrationValueSet):
    """Schema for the calibration data of the qubit"""

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
            qubit_conf["id"] = str(qubit_conf["id"]["value"]).strip("q")
            results.append(QubitCalibration.model_validate(qubit_conf))

        return results

    @classmethod
    def from_calib_results(
        cls, data: CalibrationResults, **kwargs
    ) -> List["QubitCalibration"]:
        """Generates new instances of QubitCalibration from the calibration results

        Args:
            data: the for the qubit in redis

        Returns:
            the list of new instances of QubitCalibration
        """
        results: List[QubitCalibration] = []
        for qubit, qubit_data in data.transmons.items():
            results.append(
                QubitCalibration(
                    id=int(qubit.strip("q")),
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


class ResonatorCalibration(CalibrationValueSet):
    """Schema for the calibration data of the resonator"""

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
            resonator_conf["id"] = str(resonator_conf["id"]["value"]).strip("q")
            results.append(ResonatorCalibration.model_validate(resonator_conf))

        return results

    @classmethod
    def from_calib_results(
        cls, data: CalibrationResults, **kwargs
    ) -> List["ResonatorCalibration"]:
        """Generates new instances of ResonatorCalibration from the calibration results

        Args:
            data: the calibration results

        Returns:
            the list of new instances of ResonatorCalibration
        """
        results: List[ResonatorCalibration] = []
        for qubit, item in data.transmons.items():
            results.append(
                ResonatorCalibration(
                    id=int(qubit.strip("q")),
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


class CouplerCalibration(CalibrationValueSet):
    """Schema for the calibration data of the coupler"""

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

    @classmethod
    def from_calib_config(
        cls, conf: "_BackendCalibrationConfig"
    ) -> List["CouplerCalibration"]:
        """Generates new instances of CouplerCalibration from the backend calibration config

        Args:
            conf: the backend calibration config

        Returns:
            the list of new instance of CouplerCalibration
        """
        coupler_units = conf.units.get("coupler", {})
        coupler_data = attach_units_many(conf.coupler, coupler_units)

        results: List[CouplerCalibration] = []
        for coupler_conf in coupler_data:
            coupler_conf["id"] = str(coupler_conf["id"]["value"]).strip("u")
            results.append(CouplerCalibration.model_validate(coupler_conf))

        return results

    @classmethod
    def from_calib_results(
        cls,
        data: CalibrationResults,
        name_id_map: Dict[str, int],
        reverse_phase_qubits: Tuple[str, ...] = (),
        **kwargs,
    ) -> List["CouplerCalibration"]:
        """Generates new instances of CouplerCalibration from the calibration results

        Args:
            data: the calibration results
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
        for coupler, item in data.couplers.items():
            try:
                bcc_coupler_idx = name_id_map[coupler]
            except KeyError:
                # ignore couplers missing in the map
                continue

            control_phase, target_phase = cls._get_local_phases(
                coupler, item, reverse_phase_qubits
            )

            results.append(
                CouplerCalibration(
                    id=bcc_coupler_idx,
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

    @classmethod
    def _get_local_phases(
        cls, coupler: str, data: _CouplerInRedis, reverse_phase_qubits: Tuple[str, ...]
    ) -> Tuple[float, float]:
        """Gets the local phases in radians for given the data from redis

        Args:
            coupler: the coupler name
            data: the data of the coupler as obtained from the redis
            reverse_phase_qubits: the qubits that have the reverse phase of what they sh
                should have phase on their couplers

        Returns:
            tuple of (control_qubit_local_phase, target_qubit_local_phase)
        """
        control_phase = data.cz_dynamic_control
        target_phase = data.cz_dynamic_target

        # FIXME: Major assumption: couplers are labeled as q{num}_q{num}
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

        # convert to radians
        return np.deg2rad(control_phase), np.deg2rad(target_phase)


class DeviceCalibration(Schema, CalibrationValueSet):
    """Schema for the calibration data of a given device"""

    __primary_key_fields__ = ("name",)

    name: str
    version: str
    qubits: List[QubitCalibration]
    resonators: Optional[List[ResonatorCalibration]] = None
    couplers: Optional[List[CouplerCalibration]] = None
    discriminators: Optional[Dict[str, Any]] = None
    last_calibrated: str

    def to_toml(self, file_path: PathLike[str]):
        """Persists the calibration data to a TOML file

        Args:
            file_path: the path to the TOML file to persist to
        """
        units = self.model_dump_unit(exclude_none=True)
        scalars = self.model_dump_scalars(exclude_none=True)

        qubit_units = {}
        coupler_units = {}
        resonator_units = {}

        with suppress(KeyError, IndexError):
            qubit_units = units["qubits"][0]
        with suppress(KeyError, IndexError, TypeError):
            coupler_units = units["couplers"][0]
        with suppress(KeyError, IndexError, TypeError):
            resonator_units = units["resonators"][0]
        conf = _BackendCalibrationConfig(
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

        file_data = {
            "calibration_config": conf.model_dump(mode="json"),
        }
        with open(file_path, "w") as file:
            toml.dump(file_data, file)

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
            raise ValueError(f"calibration_config is None on backend config")

        return cls(
            name=conf.general_config.name,
            version=conf.general_config.version,
            qubits=QubitCalibration.from_calib_config(calib_config),
            resonators=ResonatorCalibration.from_calib_config(calib_config),
            couplers=CouplerCalibration.from_calib_config(calib_config),
            discriminators=calib_config.discriminators,
            last_calibrated=utc_now_str(),
        )

    @classmethod
    def from_calib_results(
        cls,
        data: CalibrationResults,
        name_id_map: Dict[str, int],
        reverse_phase_qubits: Tuple[str, ...] = (),
        **kwargs: Any,
    ) -> "DeviceCalibration":
        """Generates a new instance of DeviceCalibration from the calibration results

        Args:
            data: the calibration results
            name_id_map: the map for the names of couplers/qubits
                    in calibration data to their identifier in BCC
            reverse_phase_qubits: the qubits that have their phases reversed on couplers
            kwargs: extra args like:

            kwargs: extra args to add to the DeviceCalibration

        Returns:
            the new instance of DeviceCalibration
        """
        # set defaults
        kwargs.setdefault("last_calibrated", utc_now_str())
        kwargs.setdefault("version", "0.0.0")

        return DeviceCalibration(
            **kwargs,
            qubits=QubitCalibration.from_calib_results(data),
            resonators=ResonatorCalibration.from_calib_results(data),
            couplers=CouplerCalibration.from_calib_results(
                data,
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
                    for qubit, item in data.transmons.items()
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
    online_date: str = utc_now_str()


class _BackendDeviceConfig(BaseModel):
    """The device config for the backend"""

    qubit_ids: List[str]
    discriminators: List[str] = ["lda", "thresholded_acquisition"]
    # unidirectional map of coupler to qubit pair e.g. {u1: (q1,q2), u3: (q2,q3)}
    coupling_dict: Dict[str, Tuple[str, str]] = {}

    # `qubit_ids_coupler_map` is a list of tuples of qubit couplings and their respective
    # couplers, with the qubits represented by their ids in integer form
    # e.g. [((1,2), 1), ((2,3), 3)]
    qubit_ids_coupler_map: List[Tuple[Tuple[int, int], int]] = []
    # the [x, y] coordinates of the qubits
    coordinates: List[Tuple[int, int]] = []

    # `coupling_map` is a list of bi-directional couplings with the qubits represented
    # by their indexes in the list of qubit_ids available
    # e.g. 1-to-2 coupling is represented by two tuples [1, 2], [2, 1]
    coupling_map: Optional[List[Tuple[int, int]]] = None
    meas_map: List[List[int]] = []
    qubit_parameters: List[str] = []
    resonator_parameters: List[str] = []
    coupler_parameters: List[str] = []
    discriminator_parameters: Dict[str, List[str]] = {}
    fixed_duration_couplers: Tuple[str, ...] = ()
    reverse_phase_qubits: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def set_coupling_map(self):
        coupling_dict = self.coupling_dict
        qubit_ids: List[str] = self.qubit_ids

        if len(coupling_dict) == 0:
            # special case when technically there is no coupler but there might be some qubits,
            # let each qubit be seen to couple with itself
            self.coupling_map = [(idx, idx) for idx, _ in enumerate(qubit_ids)]
        else:
            qubit_index = {_id: psn for psn, _id in enumerate(qubit_ids)}

            def get_index(str_id) -> int:
                return qubit_index[str_id]

            def get_id(str_id: str) -> int:
                return int(str_id.strip("q").strip("u"))

            # coupling_map is a list of bi-directional couplings with the qubits represented
            # by their indexes in the list of qubit_ids available
            index_couplings = [
                (get_index(q1), get_index(q2)) for q1, q2 in coupling_dict.values()
            ]
            reverse_index_couplings = [(q2, q1) for q1, q2 in index_couplings]
            self.coupling_map = index_couplings + reverse_index_couplings

            # qubit_ids_coupler_map is a list of tuples of qubit couplings and their respective
            # couplers, with the qubits represented by their ids in integer form
            id_coupling_items = [
                ((get_id(q1), get_id(q2)), get_id(c))
                for c, (q1, q2) in coupling_dict.items()
            ]
            reverse_id_coupling_items = [
                ((q2, q1), c) for (q1, q2), c in id_coupling_items
            ]
            self.qubit_ids_coupler_map = id_coupling_items + reverse_id_coupling_items

        return self


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


class BackendConfig(Schema):
    """The configration as read from the file"""

    __primary_key_fields__ = ("name",)

    name: Optional[str] = None
    general_config: _BackendGeneralConfig
    device_config: _BackendDeviceConfig
    gates: Dict[str, Dict[str, Any]] = {}
    calibration_config: Optional[_BackendCalibrationConfig] = None

    @classmethod
    def from_toml(cls, file: PathLike, seed_file: PathLike):
        """Creates a BackendConfig instance from a TOML file"""
        data = toml.load(file)

        calibration_config = None
        try:
            seed_data = toml.load(seed_file)
            calibration_config = _BackendCalibrationConfig(
                **seed_data["calibration_config"]
            )
        except (FileNotFoundError, KeyError):
            pass

        return cls(
            general_config=_BackendGeneralConfig(**data.get("general_config", {})),
            device_config=_BackendDeviceConfig(**data.get("device_config", {})),
            gates={k: {**v} for k, v in data.get("gates", {}).items()},
            calibration_config=calibration_config,
        )

    @model_validator(mode="after")
    def check_simulator_config(self):
        if self.general_config.simulator and self.calibration_config is None:
            raise ValueError("Calibration config is required for simulators.")

        return self

    @model_validator(mode="after")
    def set_name(self):
        """Sets the name basing on the general config"""
        self.name = self.general_config.name
        return self
