# This code is part of Tergite
#
# (C) Copyright Pontus Vikstål (2024)
# (C) Copyright Adilet Tuleouv (2024)
# (C) Copyright Stefan Hill (2024)
# (C) Copyright Martin Ahindura (2024)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from os import PathLike
from typing import Any, Dict, Generic, List, Literal, Optional, Tuple, TypeVar, Union

import toml
from pydantic import BaseModel, Extra, model_validator

from app.utils.datetime import utc_now_str
from app.utils.store import Schema

from .utils import attach_units_many

_CalibValueType = TypeVar("_CalibValueType", int, float, str)


class QubitProps(BaseModel):
    """Qubit Device configuration"""

    frequency: float
    pi_pulse_amplitude: float
    pi_pulse_duration: float
    pulse_type: str
    pulse_sigma: float
    t1_decoherence: float
    t2_decoherence: float
    id: Optional[int] = None
    index: Optional[int] = None
    x_position: Optional[int] = None
    y_position: Optional[int] = None
    xy_drive_line: Optional[int] = None
    z_drive_line: Optional[int] = None


class ReadoutResonatorProps(BaseModel):
    """ReadoutResonator Device configuration"""

    acq_delay: float
    acq_integration_time: float
    frequency: float
    pulse_amplitude: float
    pulse_delay: float
    pulse_duration: float
    pulse_type: str
    id: Optional[int] = None
    index: Optional[int] = None
    x_position: Optional[int] = None
    y_position: Optional[int] = None
    readout_line: Optional[int] = None
    lda_parameters: Optional[Dict[str, Any]] = None


class CouplerProps(BaseModel):
    """Coupler Device configuration"""

    frequency: float
    frequency_detuning: int
    anharmonicity: int
    coupling_strength_02: int
    coupling_strength_12: int
    cz_pulse_amplitude: float
    cz_pulse_dc_bias: float
    cz_pulse_phase_offset: float
    cz_pulse_duration_before: float
    cz_pulse_duration_rise: float
    cz_pulse_duration_constant: float
    control_rz_lambda: float
    target_rz_lambda: float
    pulse_type: str
    id: Optional[int] = None


class Device(Schema):
    """The schema for device information"""

    __primary_key_fields__ = ("name",)

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

    class Config:
        extra = Extra.allow

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


class CalibrationValue(BaseModel, Generic[_CalibValueType], extra=Extra.allow):
    """A calibration value"""

    value: Union[float, str, int]
    date: Optional[str] = None
    unit: str = ""


class QubitCalibration(BaseModel, extra=Extra.allow):
    """Schema for the calibration data of the qubit"""

    t1_decoherence: Optional[CalibrationValue[float]] = None
    t2_decoherence: Optional[CalibrationValue[float]] = None
    frequency: Optional[CalibrationValue[float]] = None
    anharmonicity: Optional[CalibrationValue[float]] = None
    readout_assignment_error: Optional[CalibrationValue[float]] = None
    # parameters for x gate
    pi_pulse_amplitude: Optional[CalibrationValue[float]] = None
    pi_pulse_duration: Optional[CalibrationValue[float]] = None
    pulse_type: Optional[CalibrationValue[str]] = None
    pulse_sigma: Optional[CalibrationValue[float]] = None
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


class ResonatorCalibration(BaseModel, extra=Extra.allow):
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


class CouplerCalibration(BaseModel, extra=Extra.allow):
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


class DeviceCalibration(Schema):
    """Schema for the calibration data of a given device"""

    __primary_key_fields__ = ("name",)

    name: str
    version: str
    qubits: List[QubitCalibration]
    resonators: Optional[List[ResonatorCalibration]] = None
    couplers: Optional[List[CouplerCalibration]] = None
    discriminators: Optional[Dict[str, Any]] = None
    last_calibrated: str

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


class BackendConfig(BaseModel):
    """The configration as read from the file"""

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
