# This code is part of Tergite
#
# (C) Chalmers Next Labs (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import json
import re
from os import PathLike
from typing import Dict, List, Mapping, Optional, Union

import qblox_instruments
import yaml
from pydantic import BaseModel, Field, RootModel, field_validator
from quantify_scheduler.backends.qblox_backend import QbloxHardwareCompilationConfig

ALLOWED_TOP_LEVEL_INSTRUMENTS = {
    "Cluster",
    "LocalOscillator",
    "IQMixer",
    "OpticalModulator",
    "SPI-Rack",
}
ALLOWED_MODULE_TYPES = {"QCM", "QRM", "QCM_RF", "QRM_RF", "QTM"}
_QBLOX_CLUSTER_TYPE_MAP: Dict[str, qblox_instruments.ClusterType] = {
    "QCM": qblox_instruments.ClusterType.CLUSTER_QCM,
    "QRM": qblox_instruments.ClusterType.CLUSTER_QRM,
    "QCM_RF": qblox_instruments.ClusterType.CLUSTER_QCM_RF,
    "QRM_RF": qblox_instruments.ClusterType.CLUSTER_QRM_RF,
}

# Regex pattern for cluster names
CLUSTER_NAME_REGEX = re.compile(r"^cluster[A-Za-z0-9]+$", re.IGNORECASE)


class CouplerMapEntry(BaseModel):
    spi_module_number: int
    dac_name: str


class ModuleConfig(BaseModel):
    instrument_type: str = Field(..., description="Module instrument type.")
    # Additional module-specific fields can be provided.

    class Config:
        extra = "allow"

    @field_validator("instrument_type")
    def validate_module_instrument_type(cls, v):
        if v not in ALLOWED_MODULE_TYPES:
            raise ValueError(
                f"Invalid module instrument_type '{v}'. Allowed types: {ALLOWED_MODULE_TYPES}"
            )
        return v


class InstrumentConfig(BaseModel):
    """
    Represents a top-level instrument configuration.
    For instrument_type 'Cluster', additional fields 'ip_address' and 'is_dummy'
    are expected along with an optional 'modules' dictionary.
    """

    instrument_type: str = Field(..., description="Top-level instrument type.")
    ref: Optional[str] = None
    modules: Optional[Dict[str, ModuleConfig]] = None

    # New fields for Cluster instruments
    ip_address: Optional[str] = Field(
        None, description="IP address for a Cluster instrument."
    )
    is_dummy: Optional[bool] = Field(
        None, description="Indicates if the cluster is a dummy cluster."
    )
    port: Optional[str] = None
    coupler_spi_mapping: Optional[Dict[str, CouplerMapEntry]] = None

    class Config:
        extra = "allow"

    @field_validator("instrument_type")
    def validate_instrument_type(cls, v):
        if v not in ALLOWED_TOP_LEVEL_INSTRUMENTS:
            raise ValueError(
                f"Invalid top-level instrument_type '{v}'. Allowed types: {ALLOWED_TOP_LEVEL_INSTRUMENTS}"
            )
        return v


class QuantifyMetadata(RootModel[Dict[str, InstrumentConfig]]):
    """
    Quantify-specific metadata got from quantify-metadata.yml
    """

    @field_validator("root")
    def validate_hardware_description(
        cls, v: Dict[str, InstrumentConfig]
    ) -> Dict[str, InstrumentConfig]:
        for name, instr in v.items():
            if instr.instrument_type == "Cluster":
                # Validate that for each Cluster instrument the name matches the expected pattern.
                if not CLUSTER_NAME_REGEX.match(name):
                    raise ValueError(
                        f"Cluster name '{name}' does not match expected pattern 'cluster<number>'."
                    )
                # Validate that for each Cluster instrument has an ip address.
                if not instr.ip_address:
                    raise ValueError(f"Cluster '{name}' must specify an ip_address.")
            elif instr.instrument_type == "SPI-Rack":
                if not instr.port:
                    raise ValueError(f"SPI-Rack '{name}' must specify a serial 'port'.")
        return v

    @classmethod
    def from_yaml(cls, file_path: Union[PathLike, str]) -> "QuantifyMetadata":
        """Loads the metadata from a YAML file

        Args:
            file_path: the path to the YAML file

        Returns:
            the QuantifyMetadata represented in the YAML file
        """
        with open(file_path, "r") as file:
            data = yaml.safe_load(file)

        return cls.model_validate(data)

    def get_clusters(self) -> List[qblox_instruments.Cluster]:
        """Get the clusters corresponding to the metadata

        Returns:
            the list of clusters got from this metadata
        """

        return [
            _create_cluster(name, conf)
            for name, conf in self.root.items()
            if conf.instrument_type == "Cluster"
        ]


def load_quantify_config(
    file_path: Union[PathLike, str],
) -> QbloxHardwareCompilationConfig:
    """Loads the quantify config json file to QbloxHardwareCompilationConfig

    Args:
        file_path: path to the quantify config json file

    Returns:
        the QbloxHardwareCompilationConfig got from the quantify json file
    """
    with open(file_path) as file:
        data = json.load(file)

    # new_config = QbloxHardwareCompilationConfig.from_old_style_hardware_config(data)
    return QbloxHardwareCompilationConfig.model_validate(data)


def _create_cluster(name: str, conf: InstrumentConfig) -> qblox_instruments.Cluster:
    """
    Creates and initializes a Cluster object.

    If the configuration indicates a dummy cluster, a dummy configuration is built.
    Otherwise, a real cluster is instantiated and reset.

    Args:
        name: the name of the cluster
        conf: the configuration of the cluster

    Returns:
        qblox_instruments.Cluster for given name and config
    """
    dummy_cfg = None
    if getattr(conf, "is_dummy", False):
        dummy_cfg = {
            int(idx): _QBLOX_CLUSTER_TYPE_MAP[conf.instrument_type]
            for idx, conf in conf.modules.items()
        }


    return qblox_instruments.Cluster(
        name=name,
        identifier=conf.ip_address,
        dummy_cfg=dummy_cfg,
    )
