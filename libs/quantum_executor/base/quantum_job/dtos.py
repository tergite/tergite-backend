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
from typing import Any, List, Optional, Type, Union

import numpy as np
from pydantic import BaseModel, ConfigDict

from libs.qiskit.qobj import PulseQobj, PulseQobjConfig
from libs.qiskit_providers.utils import json_decoder
from libs.qiskit_providers.utils.json_encoder import IQXJsonEncoder

from ..circuits import CircuitJobParams, CircuitReadout
from .typing import QJobResult


class BinMode(str, Enum):
    """
    Describes how to handle `Acquisitions` that write to the same `AcquisitionIndex`.
    """

    # Repository: https://gitlab.com/quantify-os/quantify-scheduler
    # Licensed according to the LICENCE file on the main branch
    # BSD 3-Clause License
    #
    # Copyright (c) 2020-2024, The Quantify consortium.
    # All rights reserved.
    #
    # Redistribution and use in source and binary forms, with or without modification,
    # are permitted provided that the following conditions are met:
    #
    # 1. Redistributions of source code must retain the above copyright notice,
    #   this list of conditions and the following disclaimer.
    #
    # 2. Redistributions in binary form must reproduce the above copyright notice,
    #   this list of conditions and the following disclaimer in the documentation
    #   and/or other materials provided with the distribution.
    #
    # 3. Neither the name of the copyright holder nor the names of its contributors
    #   may be used to endorse or promote products derived from this software without
    #   specific prior written permission.
    #
    # THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
    # AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
    # THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
    # IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
    # SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    # SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
    # HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
    # (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
    # EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

    APPEND = "append"
    AVERAGE = "average"
    FIRST = "first"
    DISTRIBUTION = "distribution"
    SUM = "sum"


class MeasLvl(int, Enum):
    """How far measurements are processed: to discriminated states, IQ points or raw"""

    DISCRIMINATED = 2
    INTEGRATED = 1
    RAW = 0


class MeasRet(int, Enum):
    """Whether the measurements of all shots are averaged or returned shot by shot"""

    AVERAGED = 1
    APPENDED = 0


class MeasProtocol(str, enum.Enum):
    """How each measurement is acquired: integrated to an IQ point or as a trace"""

    SSB_INTEGRATION_COMPLEX = "SSBIntegrationComplex"
    TRACE = "trace"


class AcqReturnType(int, enum.Enum):
    """The serializable name of the type an acquisition returns"""

    COMPLEX = 1
    ND_ARRAY = 2

    def to_type(self) -> Union[Type]:
        """Retrieves the type associated with this enum

        Returns:
            complex or numpy.ndarray
        """
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
        """Converts this config into a JSON serializable dictionary

        Returns:
            the JSON serializable dict of this config
        """
        raw_dict = asdict(self)
        if self.acq_return_type == complex:
            raw_dict["acq_return_type"] = AcqReturnType.COMPLEX
        elif self.acq_return_type == np.ndarray:
            raw_dict["acq_return_type"] = AcqReturnType.ND_ARRAY
        return raw_dict

    @classmethod
    def from_pulse_qobj_config(cls, config: PulseQobjConfig) -> "NativeQobjConfig":
        """Gets the native config of the pulse qobj config

        Args:
            config: the configuration object of the pulse qobj

        Returns:
            NativeQobjConfig instance
        """
        return cls.from_measurement_settings(
            meas_level=config.meas_level,
            meas_return=config.meas_return,
            shots=config.shots,
            n_qubits=config.n_qubits,
        )

    @classmethod
    def from_circuit_job_params(
        cls, params: CircuitJobParams, n_qubits: int
    ) -> "NativeQobjConfig":
        """Gets the native config of a gate-level job, which returns every shot of its
        discriminated measurements, whatever its meas_return

        Args:
            params: the params of the gate-level job
            n_qubits: the number of the device's qubits

        Returns:
            NativeQobjConfig instance
        """
        meas_return = params.meas_return
        if params.meas_level == MeasLvl.DISCRIMINATED:
            meas_return = "single"
        return cls.from_measurement_settings(
            meas_level=params.meas_level,
            meas_return=meas_return,
            shots=params.shots,
            n_qubits=n_qubits,
        )

    @classmethod
    def from_measurement_settings(
        cls,
        meas_level: int,
        meas_return: Union[int, str],
        shots: int,
        n_qubits: int,
    ) -> "NativeQobjConfig":
        """Gets the native config of the measurement settings of a job

        Args:
            meas_level: how far the measurements are processed, 0, 1 or 2
            meas_return: whether the shots are averaged, as a MeasRet or its name
            shots: the number of shots
            n_qubits: the number of qubits

        Returns:
            NativeQobjConfig instance

        Raises:
            RuntimeError: Combination {(bin_mode, protocol)} is not supported.
        """
        bin_mode = _get_bin_mode(meas_return)
        protocol = _get_meas_protocol(meas_level)
        meas_level = MeasLvl(meas_level)

        if (
            bin_mode is BinMode.AVERAGE
            and protocol is MeasProtocol.SSB_INTEGRATION_COMPLEX
        ):
            return cls(
                acq_return_type=complex,
                protocol=protocol,
                bin_mode=bin_mode,
                meas_level=meas_level,
                meas_return=MeasRet.AVERAGED,
                meas_return_cols=1,
                n_qubits=n_qubits,
                shots=shots,
            )

        if bin_mode is BinMode.AVERAGE and protocol is MeasProtocol.TRACE:
            return cls(
                acq_return_type=np.ndarray,
                protocol=protocol,
                bin_mode=bin_mode,
                meas_level=meas_level,
                meas_return=MeasRet.AVERAGED,
                meas_return_cols=16384,  # length of a trace
                n_qubits=n_qubits,
                shots=shots,
            )

        if (
            bin_mode is BinMode.APPEND
            and protocol is MeasProtocol.SSB_INTEGRATION_COMPLEX
        ):
            return cls(
                acq_return_type=np.ndarray,
                protocol=MeasProtocol.SSB_INTEGRATION_COMPLEX,
                bin_mode=BinMode.APPEND,
                meas_level=meas_level,
                meas_return=MeasRet.APPENDED,
                meas_return_cols=shots,
                n_qubits=n_qubits,
                shots=shots,
            )

        raise RuntimeError(f"Combination {(bin_mode, protocol)} is not supported.")

    @classmethod
    def from_dict(cls, value: dict) -> "NativeQobjConfig":
        """Converts a dict into a NativeQobjConfig object

        Args:
            value: the dictionary to convert

        Returns:
            the NativeQobjConfig object
        """
        acq_return_type = value["acq_return_type"].to_type()
        value = value | {"acq_return_type": acq_return_type}
        return cls(**value)


class ByteOrder(str, enum.Enum):
    """Whether a bit string lists the acquisition channels most or least significant first"""

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
        """Converts this metadata into a JSON serializable dictionary

        Returns:
            the JSON serializable dict of this metadata
        """
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
        """Converts this wrapper into a JSON serializable dictionary

        Returns:
            the JSON serializable dict of this wrapper
        """
        return asdict(self)

    def to_qobj(self) -> PulseQobj:
        """Returns a PulseQobj instance from this QobjData

        Returns:
            the qobject this wrapper holds
        """
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
    qobj: Optional[PulseQobj] = None
    raw_results: Optional[QJobResult] = None
    # a gate-level job's, in the order of its experiments, in place of a qobj
    readouts: Optional[List[CircuitReadout]] = None
    # a gate-level job's shots of each circuit; none of a qobj's, or of a file saved before
    shots: Optional[int] = None

    @property
    def is_local(self) -> bool:
        """Whether the job runs with no job id"""
        return self.job_id is None

    @property
    def metadata(self) -> Optional[QobjMetadata]:
        """The metadata of the job's qobj, if it has one"""
        if self.qobj is None:
            return None
        return QobjMetadata.from_qobj(self.qobj)

    def to_dict(self):
        """Converts this job into a dictionary

        Returns:
            the dict of this job's fields
        """
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


def _get_bin_mode(meas_return: Union[int, str]) -> BinMode:
    """Gets the BinMode of a job's meas_return

    Args:
        meas_return: the meas_return, as a MeasRet or its name

    Returns:
        the BinMode for the given meas_return
    """
    if isinstance(meas_return, int):
        return {
            int(MeasRet.APPENDED): BinMode.APPEND,
            int(MeasRet.AVERAGED): BinMode.AVERAGE,
        }[meas_return]

    # FIXME: For some reason, PulseQobjConfig expects to be an int
    #   yet our fixtures all have strings.
    meas_return = str.lower(meas_return)
    return {
        "avg": BinMode.AVERAGE,
        "average": BinMode.AVERAGE,
        "averaged": BinMode.AVERAGE,
        "single": BinMode.APPEND,
        "append": BinMode.APPEND,
        "appended": BinMode.APPEND,
    }[meas_return]


def _get_meas_protocol(meas_level: int) -> "MeasProtocol":
    """Gets the measurement protocol of a job's meas_level

    Args:
        meas_level: the meas_level, 0, 1 or 2

    Returns:
        the measurement protocol for the given meas_level
    """
    return {
        0: MeasProtocol.TRACE,
        1: MeasProtocol.SSB_INTEGRATION_COMPLEX,
        2: MeasProtocol.SSB_INTEGRATION_COMPLEX,
    }[meas_level]
