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

# This code is part of Tergite
#
# (C) Copyright Tong Liu 2024
# (C) Copyright Chalmers Next Labs 2025
# (C) Copyright Michele Faucci Giannelli 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Modification notice:
# This code was copied from tergite-autocalibration package
# and modified to adapt and integrate into tergite-backend package in Jul 2025

import sys
import time
from pathlib import Path
import logging
import os

import numpy as np
from qblox_instruments import SpiRack
from qcodes import validators
from rich.progress import Progress
from typing import Tuple, Dict, Any


import settings

from app.libs.quantum_executor.utils.config import QuantifyMetadata
from ..utils.config import CouplerMapEntry

# TODO: 2. Set up REDIS_CONNECTION and plan for it's use for storing/fetching parking currents
# TODO: 8. Make safety ranges configurable

logger = logging.getLogger(__name__)
REDIS_CONNECTION = settings.REDIS_CONNECTION


def _find_and_validate_spi_port(port: str | None) -> str | None:
    """
    Verify that port is reachable on the current machine.

    * On Windows we just return the value the user gave us
    * On POSIX we only check that the device node exists under ``/dev``.

    Parameters:
    port
        Serial port taken from *metadata.yml*, e.g. ``/dev/ttyACM0`` or
        ``COM3``.  If ``None`` the function logs a warning and returns *None*.

    Returns:
    str | None
        The validated port string, or *None* if the check fails.
    """
    if port is None:
        logger.warning("No SPI port configured.")

    if os.name == "nt":
        # assume user running on windows and set up port properly
        return port

    # otherwiese POSIX
    dev_path = Path(port)
    if dev_path.exists():
        return port

    # For the default base case, return None
    logger.warning(
        "Couldn't find the serial port of the SPI rack. "
        "Please check cable or fix the entry in metadata.yml",
        port,
    )
    return None


def _get_spi_metadata(
    metadata_path: str | Path,
) -> Tuple[str | None, bool, Dict[str, CouplerMapEntry]]:
    """
    Extract (port, is_dummy, coupler_map) from the first `SPI-Rack`
    instrument found in *metadata_path*.

    If the rack is missing, returns (None, False, {}).
    """
    meta = QuantifyMetadata.from_yaml(metadata_path)

    for conf in meta.root.values():
        if conf.instrument_type.lower().replace("_", "-") == "spi-rack":
            port = getattr(conf, "port", None)
            is_dummy = bool(getattr(conf, "is_dummy", False))
            mapping = getattr(conf, "coupler_spi_mapping", {}) or {}
            return port, is_dummy, mapping

    return None, False, {}


class SpiDAC:
    def __init__(
        self,
        couplers: list[str],
        metadata_path: str | Path = settings.QUANTIFY_METADATAFILE,
    ):
        # grab spi metadata
        raw_port, self.is_dummy, self._coupler_map = _get_spi_metadata(metadata_path)

        # validate port unless running dummy
        self.port = (
            _find_and_validate_spi_port(raw_port) if not self.is_dummy else raw_port
        )

        if self.port is None and not self.is_dummy:
            raise ValueError(
                "SPI rack port not found and 'is_dummy' is false - "
                "fix the entry in metadata.yml."
            )

        # connect or build dummy
        self.spi = SpiRack(settings.DEFAULT_PREFIX, self.port, is_dummy=self.is_dummy)

        # build DAC handles
        self.dacs_dictionary: Dict[str, Any] = {
            coupler: self._create_spi_dac(coupler) for coupler in couplers
        }

    def create_spi_dac(self, coupler: str):

        try:
            entry = self._coupler_map[coupler]
        except KeyError:
            raise KeyError(
                f"Coupler '{coupler}' missing in metadata.yml under 'coupler_spi_mapping'."
            )

        spi_mod_number = entry.spi_module_number
        dac_name = entry.dac_name
        spi_mod_name = f"module{spi_mod_number}"

        if self.is_dummy:
            return f"Dummy_DAC_for_{spi_mod_name}_{dac_name}"

        if spi_mod_name not in self.spi.instrument_modules:
            self.spi.add_spi_module(spi_mod_number, "S4g")

        this_dac = self.spi.instrument_modules[spi_mod_name].instrument_modules[
            dac_name
        ]

        # WARNING: this command is bugged on the SPI firmware. When a DAC in operated
        # WARNING: for the first time, it sets the current to the minimum -0.25mA, which causes
        # WARNING: significant and dangerous heating. Follow the group instructions when you
        # WARNING: want to operate a DAC for the first time, or after a restart of the SPI rack.
        this_dac.span("range_min_bi")

        this_dac.current.vals = validators.Numbers(min_value=-3.1e-3, max_value=3.1e-3)
        this_dac.ramping_enabled(True)
        this_dac.ramp_rate(40e-6)
        this_dac.ramp_max_step(1e-6)
        return this_dac

    def set_dacs_zero(self) -> None:
        self.spi.set_dacs_zero()
        return

    def set_parking_currents(self, couplers: list[str]) -> None:

        parking_currents = {}
        # TODO: Change message about zero currents in device_config.toml - tergite-backend has backend_config.toml instead
        for coupler in couplers:
            if REDIS_CONNECTION.hexists(f"couplers:{coupler}", "parking_current"):
                parking_current = float(
                    REDIS_CONNECTION.hget(f"couplers:{coupler}", "parking_current")
                )
            else:
                message = (
                    "parking current is not present on redis."
                    "If you intend to operate at zero DC current, set a zero value at your device_config.toml"
                )
                logger.warning(f"{message}")
                raise ValueError(message)

            parking_currents[coupler] = parking_current

        self.set_dac_current(parking_currents)
        return

    def set_dac_current(self, dac_values: dict[str, float]) -> None:
        if self.is_dummy:
            logger.status(
                f"Dummy DAC to current {dac_values}. NO REAL CURRENT is generated"
            )
            return
        self.ramp_current_serially(dac_values)

    def ramp_current_simultaneusly(self, dac_values: dict[str, float]):
        for coupler, target_current in dac_values.items():
            dac = self.dacs_dictionary[coupler]
            dac.current(target_current)
        ramp_counter = 0
        couplers = self.dacs_dictionary.keys()
        dacs = self.dacs_dictionary.values()
        logger.status(f"{'Ramping current (mA)'}")
        logger.status(f"{couplers}", end=": ")
        while any([dac.is_ramping() for dac in dacs]):
            ramp_counter += 1
            print_termination = " -> "
            if ramp_counter % 8 == 0:
                print_termination = "\n"
            these_currents = np.array([dac.current() for dac in dacs])
            sys.stdout.write(f"{these_currents * 1000}", end=print_termination)
            sys.stdout.flush()
            time.sleep(1)
        logger.status(f"Ramping finished at {dac.current() * 1000:.4f} mA")

    def ramp_current_serially(self, dac_values: dict[str, float]):
        for coupler, target_current in dac_values.items():
            dac = self.dacs_dictionary[coupler]
            initial_current = dac.current() * 1000  # Convert to mA
            target_mA = target_current * 1000  # Convert to mA
            total_range = abs(target_mA - initial_current)  # Compute range for progress
            if total_range == 0:
                continue  # Already at target, no need to ramp

            dac.current(target_current)

            with Progress() as progress:
                task = progress.add_task("Ramping current...", total=total_range)

                while dac.is_ramping():
                    try:
                        current_mA = dac.current() * 1000  # Get current in mA
                        progress.update(
                            task,
                            completed=abs(current_mA - initial_current),
                            description=f"Coupler {coupler}: current is {current_mA:.4f} with target {target_mA:.4f} mA",
                        )

                        if (
                            abs(current_mA - target_mA) < 0.005
                        ):  # Stop when close enough
                            break

                        time.sleep(0.5)  # Simulate delay

                    except ValueError as e:
                        progress.stop()
                        logger.error(f"Error reading DAC current: {e}")
                        break

        logger.status(f"Ramping finished")

    def print_currents(self):
        for coupler, dac in self.dacs_dictionary.items():
            current = dac.current() * 1000
            logger.info(f"{coupler}: {current:.4f} mA")

    def close_spi_rack(self):
        self.spi.close()
        logger.status(f"Closing SPI rack")
