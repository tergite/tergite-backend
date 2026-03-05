# This code is part of Tergite
#
# (C) Axel Andersson (2022)
# (C) Martin Ahindura (2024)
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Data Transfer objects for the Quantum job"""
import enum
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional, Type, Union

import numpy as np
from pydantic import BaseModel, ConfigDict
from qiskit.qobj import PulseQobj
from quantify_scheduler.enums import BinMode

from app.libs.qiskit_providers.utils import json_decoder
from app.libs.qiskit_providers.utils.json_encoder import IQXJsonEncoder

from .typing import QJobResult


class MeasLvl(int, Enum):
    DISCRIMINATED = 2
    INTEGRATED = 1
    RAW = 0


class MeasRet(int, Enum):
    AVERAGED = 1
    APPENDED = 0


class MeasProtocol(str, enum.Enum):
    SSB_INTEGRATION_COMPLEX = "SSBIntegrationComplex"
    TRACE = "trace"


class AcqReturnType(int, enum.Enum):
    COMPLEX = 1
    ND_ARRAY = 2

    def to_type(self) -> Union[Type]:
        """Retrieves the type associated with this enum"""
        if self == AcqReturnType.COMPLEX:
            return complex
        elif self == AcqReturnType.ND_ARRAY:
            return np.ndarray


@dataclass(frozen=True)
class NativeQobjConfig:
    """Settings for running native experiments"""

    acq_return_type: Union[Type[complex], Type[np.ndarray]]
    protocol: MeasProtocol
    bin_mode: BinMode
    meas_level: MeasLvl
    meas_return: MeasRet
    meas_return_cols: int
    n_qubits: int
    shots: int

    def to_dict(self) -> dict:
        """Converts this config into a JSON serializable dictionary"""
        raw_dict = asdict(self)
        if self.acq_return_type == complex:
            raw_dict["acq_return_type"] = AcqReturnType.COMPLEX
        elif self.acq_return_type == np.ndarray:
            raw_dict["acq_return_type"] = AcqReturnType.ND_ARRAY
        return raw_dict

    @classmethod
    def from_dict(cls, value: dict) -> "NativeQobjConfig":
        """Converts a dict into a NativeQobjConfig object

        Args:
            value: the dictionary to convert

        Returns:
            the NativeQobjConfig object
        """
        acq_return_type = value["acq_return_type"].to_type()
        value = {**value, "acq_return_type": acq_return_type}
        return cls(**value)


class ByteOrder(str, enum.Enum):
    BIG_ENDIAN = "big-endian"
    LITTLE_ENDIAN = "little-endian"


@dataclass(frozen=True)
class QobjMetadata:
    """Metadata on a Qobject instance"""

    shots: int
    qobj_id: str
    num_experiments: int

    @classmethod
    def from_qobj(cls, qobj: PulseQobj):
        """Constructs the metadata from the qobject
        Args:
            qobj: the qobject whose metadata is to be obtained

        Returns:
            the QobjectMetadata for the given qobject
        """
        return cls(
            shots=qobj.config.shots,
            qobj_id=qobj.qobj_id,
            num_experiments=len(qobj.experiments),
        )

    def to_dict(self) -> dict:
        """Converts current to dictionary"""
        return asdict(self)


@dataclass(frozen=True)
class QobjData:
    """The schema for the wrapper of the raw Qobj JSON string"""

    experiment_data: str

    @classmethod
    def from_qobj(cls, qobj: PulseQobj):
        """Constructs the Qobj wrapper from the qobject
        Args:
            qobj: the qobject whose metadata is to be obtained

        Returns:
            the Qobject wrapper for the given qobject
        """
        experiment_data = json.dumps(qobj.to_dict(), cls=IQXJsonEncoder, indent="\t")
        return cls(
            experiment_data=experiment_data,
        )

    def to_dict(self) -> dict:
        """Converts current to dictionary"""
        return asdict(self)

    def to_qobj(self) -> PulseQobj:
        """Returns a PulseQobj instance from this QobjData"""
        qobj_dict = json.loads(self.experiment_data)
        json_decoder.decode_pulse_qobj(qobj_dict)
        return PulseQobj.from_dict(qobj_dict)


@dataclass
class QuantumJob:
    """Schema of the job data sent from the client"""

    tuid: str
    meas_return: MeasRet
    meas_level: MeasLvl
    meas_return_cols: int
    n_qubits: int
    job_id: Optional[str] = None
    memory_slot_size: int = 100
    local: bool = True
    qobj: Optional[PulseQobj] = None
    qobj_data: Optional[QobjData] = None
    metadata: Optional[QobjMetadata] = None
    raw_results: Optional[QJobResult] = None

    def __post_init__(self):
        """Initialize properties that depend on other properties"""
        self.local = self.job_id is None

        if isinstance(self.qobj, PulseQobj):
            self.metadata = QobjMetadata.from_qobj(self.qobj)

        if isinstance(self.qobj, PulseQobj) and self.qobj_data is None:
            self.qobj_data = QobjData.from_qobj(self.qobj)

        if isinstance(self.qobj, PulseQobj) and self.metadata is None:
            self.metadata = QobjMetadata.from_qobj(self.qobj)

        if isinstance(self.qobj_data, QobjData) and self.qobj is None:
            self.qobj = self.qobj_data.to_qobj()

    def to_dict(self):
        """Converts current to dictionary"""
        return asdict(self)


class QobjHeaderMetadata(BaseModel):
    """The metadata on the QobjHeader"""

    model_config = ConfigDict(extra="ignore")

    backend_name: Optional[str] = None

    @classmethod
    def from_qobj_header(cls, qobj_header_dict: dict):
        """Initializes an instance of this class from a qobj header dict

        Args:
            qobj_header_dict: the dict got from QobjHeader.to_dict()

        Returns:
            An instance of this class as obtained from the qobj
        """
        return cls(**qobj_header_dict)


class QobjSweepData(BaseModel):
    """The metadata on the sweep data in the QobjHeader"""

    model_config = ConfigDict(extra="ignore")

    dataset_name: Optional[str] = None
    serial_order: Optional[Any] = None
    batch_size: int = 1

    @classmethod
    def from_qobj_header(cls, qobj_header_dict: dict):
        """Initializes an instance of this class from a qobj header dict

        Args:
            qobj_header_dict: the dict got from QobjHeader.to_dict()

        Returns:
            An instance of this class as obtained from the qobj

        Raises:
            ValueError: QobjHeader lacks sweep data
        """
        try:
            sweep_data = qobj_header_dict["sweep"]
            return cls(**sweep_data)
        except KeyError:
            raise ValueError("QobjHeader lacks sweep data")

    @property
    def metadata(self) -> dict:
        """The metadata of the given sweep data"""
        return {
            "dataset_name": self.dataset_name,
            "serial_order": self.serial_order,
            "batch_size": self.batch_size,
        }


class SweepParamMetadata(BaseModel):
    """The sweep param as obtained from the qobj header"""

    model_config = ConfigDict(extra="ignore")

    long_name: str
    unit: str
