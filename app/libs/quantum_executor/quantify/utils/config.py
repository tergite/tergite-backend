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
from typing import Dict, Optional, Union

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
)
from quantify_scheduler.backends.qblox_backend import QbloxHardwareCompilationConfig

ALLOWED_TOP_LEVEL_INSTRUMENTS = {
    "Cluster",
    "LocalOscillator",
    "IQMixer",
    "OpticalModulator",
    "SPI-Rack",
}
ALLOWED_MODULE_TYPES = {"QCM", "QRM", "QCM_RF", "QRM_RF", "QTM"}
# Regex pattern for cluster names
CLUSTER_NAME_REGEX = re.compile(r"^cluster[A-Za-z0-9]+$", re.IGNORECASE)
SPI_RACK_INSTRUMENT_TYPE = "SPI-Rack"
CLUSTER_INSTRUMENT_TYPE = "Cluster"


class ModuleConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    instrument_type: str = Field(..., description="Module instrument type.")
    # Additional module-specific fields can be provided.

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

    model_config = ConfigDict(extra="allow")

    instrument_type: str = Field(..., description="Top-level instrument type.")
    ref: Optional[str] = None
    modules: Optional[Dict[str, ModuleConfig]] = None
    ip_address: Optional[str] = Field(
        None, description="IP address for a Cluster instrument."
    )
    is_dummy: Optional[bool] = Field(
        None, description="Indicates if the cluster is a dummy cluster."
    )
    port: Optional[str] = None
    experiment_delay: float = Field(
        0.0,
        description=(
            "Delay in seconds to wait between consecutive experiments within a single job. "
            "Useful to avoid back-to-back hardware execution without a cooldown interval."
        ),
    )

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
            if instr.instrument_type == CLUSTER_INSTRUMENT_TYPE:
                # Validate that for each Cluster instrument the name matches the expected pattern.
                if not CLUSTER_NAME_REGEX.match(name):
                    raise ValueError(
                        f"Cluster name '{name}' does not match expected pattern 'cluster<number>'."
                    )
                # Validate that for each Cluster instrument has an ip address.
                if not instr.ip_address:
                    raise ValueError(f"Cluster '{name}' must specify an ip_address.")
            elif instr.instrument_type == SPI_RACK_INSTRUMENT_TYPE:
                if not instr.port:
                    raise ValueError(
                        f"{SPI_RACK_INSTRUMENT_TYPE} '{name}' must specify a serial 'port'."
                    )
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

    @property
    def experiment_delay(self) -> float:
        """Delay in seconds between consecutive experiments, taken from the first cluster config."""
        for conf in self.root.values():
            if conf.instrument_type == CLUSTER_INSTRUMENT_TYPE:
                return conf.experiment_delay
        return 0.0


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
