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

import logging
import time
from typing import Dict

from qblox_instruments import SpiRack
from qcodes import Instrument, validators
from qcodes.instrument import InstrumentModule

from .utils.config import (
    SPI_RACK_INSTRUMENT_TYPE,
    CouplerMapEntry,
    QuantifyMetadata,
    SpiRackConfig,
)

# TODO: 8. Make safety ranges configurable

logger = logging.getLogger(__name__)


def init_spi_dacs(
    metadata: QuantifyMetadata,
    should_print_progress: bool = False,
) -> Dict[str, "SpiDAC"]:
    """Initializes the SpiDACs defined in the Quantify metadata

    Args:
        metadata: Quantify metadata
        should_print_progress: whether the progress should be printed when controlling the couplers; default = False

    Returns:
        the dictionary of name and SPI-Rack configurations
    """
    return {
        k: SpiDAC(
            name=k,
            conf=SpiRackConfig.model_validate(obj.model_dump()),
            should_print_progress=should_print_progress,
        )
        for k, obj in metadata.root.items()
        if obj.instrument_type == SPI_RACK_INSTRUMENT_TYPE
    }


class SpiDAC:
    """The controller for the SPI Digital Analogue Converter (DAC) that drives the magnetic flux on the couplers

    The flux is proportional to the current and thus this controls the currents applied to the couplers.
    The SPI DAC is able to convert digital instructions on what the currents should be, into the actual
    analogue currents that will flow to the couplers
    """

    def __init__(
        self,
        name: str,
        conf: SpiRackConfig,
        should_print_progress: bool = False,
    ):
        """
        Args:
            name: The name of the SPI DAC
            conf: The SPI config for this rack as got from the quantify metadata file
            should_print_progress: whether the progress should be printed when controlling the couplers; default = False

        Raises:
            ConfigurationError: Couldn't find the serial port {port} of the SPI rack.
        """
        self.name = name
        self._should_print_progress = should_print_progress
        self._config = conf
        self.port = self._config.port
        self.is_dummy = self._config.is_dummy
        self.parking_current = self._config.parking_current
        self.coupler_map = self._config.coupler_spi_mapping
        self.couplers = sorted(self.coupler_map.keys())
        try:
            self.spi_rack = SpiRack.find_instrument(name, SpiRack)
            self.coupler_dac_module_map = {
                k: _get_spi_module(self.spi_rack, v, self.is_dummy)
                for k, v in self.coupler_map.items()
            }
        except KeyError:
            self.spi_rack = SpiRack(name, self.port, is_dummy=self.is_dummy)
            self.coupler_dac_module_map = {
                k: _init_coupler_spi_module(self.spi_rack, v, self.is_dummy)
                for k, v in self.coupler_map.items()
            }

    @classmethod
    def exist(cls, name: str, instrument_class: type[Instrument] | None = None) -> bool:
        """Checks if the SpiDAC of the given name and given instrument class exists

        Args:
            name: SPI rack name
            instrument_class: Instrument class

        Returns:
            True if the SpiDAC of the given name exists
        """
        return SpiRack.exist(name, instrument_class)

    def reset_to_parking_current(self) -> None:
        """Sets the current of all couplers on this SPI to the parking current"""
        return self.ramp_to_target_currents(
            {k: self.parking_current for k in self.couplers}
        )

    def get_current_biases(self) -> Dict[str, float]:
        """Gets the current bias for all couplers on this SPI

        Returns:
            A dictionary mapping coupler name to current bias value
        """
        return {
            coupler: dac_module.current()
            for coupler, dac_module in self.coupler_dac_module_map.items()
            if isinstance(dac_module, InstrumentModule)
        }

    def ramp_to_target_currents(self, coupler_current_map: dict[str, float]):
        """Raises or drops the current from the current to the target for each coupler in the map

        Args:
            coupler_current_map: map of the coupler and the target currents to ramp to
        """
        if self.is_dummy:
            logger.info(
                "Dummy DAC to current %s. NO REAL CURRENT is generated",
                coupler_current_map,
            )
            return

        for coupler, target_current in coupler_current_map.items():
            if coupler not in self.coupler_dac_module_map:
                logger.info(
                    f"coupler {coupler} is not in spi rack {self.name}. Probably in another SPI rack."
                )
                continue

            dac_module = self.coupler_dac_module_map[coupler]
            initial_milliamps = dac_module.current() * 1000
            target_milliamps = target_current * 1000
            total_range = abs(
                target_milliamps - initial_milliamps
            )  # Compute range for progress bar
            if total_range == 0:
                continue  # Already at target, no need to ramp

            dac_module.current(target_current)
            while dac_module.is_ramping():
                try:
                    current_milliamps = dac_module.current() * 1000  # Get current in mA
                    if self._should_print_progress:
                        progress = abs(
                            (current_milliamps - initial_milliamps) / total_range * 100
                        )
                        logger.info(
                            f"Coupler {coupler}: current={current_milliamps:.4f}mA, target={target_milliamps:.4f}mA, completed={progress} %"
                        )

                    if (
                        abs(current_milliamps - target_milliamps) < 0.005
                    ):  # Stop when close enough
                        break

                    time.sleep(0.5)  # Simulate delay

                except ValueError as e:
                    logger.error(f"Error reading DAC current: {e}")
                    break

        logger.info(f"Ramping finished")

    def close(self):
        """Closes this instance and releases the resources attached to it"""
        if not self.is_dummy:
            for dac in self.coupler_dac_module_map.values():
                while bool(dac.is_ramping()):
                    time.sleep(0.05)

                dac.ramping_enabled(False)  # future sets are instant
        time.sleep(0.05)

        try:
            self.spi_rack.close()
            logger.info(f"Closing SPI rack")
        except AttributeError:
            pass


def _init_coupler_spi_module(
    spi_rack: SpiRack, coupler_map_entry: CouplerMapEntry, is_dummy: bool = False
) -> InstrumentModule | str:
    """Initializes the SPI comodule for the given coupler map entry

    Args:
        spi_rack: SpiRack instance
        coupler_map_entry: Coupler mapping entry from the metadata
        is_dummy: Whether the SPI Rack module should be initialized as a dummy SPI rack module

    Returns:
           the initialized SPI module or just a string if it is dummy
    """
    spi_mod_number = coupler_map_entry.spi_module_number
    dac_name = coupler_map_entry.dac_name
    module_name = f"module{spi_mod_number}"

    if is_dummy:
        return f"Dummy_DAC_for_{module_name}_{dac_name}"

    if module_name not in spi_rack.instrument_modules:
        spi_rack.add_spi_module(spi_mod_number, "S4g", name=module_name)

    dac = spi_rack.instrument_modules[module_name].instrument_modules[dac_name]

    # WARNING: this command is bugged on the SPI firmware. When a DAC in operated
    # WARNING: for the first time, it sets the current to the minimum -0.25mA, which causes
    # WARNING: significant and dangerous heating. Follow the group instructions when you
    # WARNING: want to operate a DAC for the first time, or after a restart of the SPI rack.
    dac.span("range_min_bi")

    dac.current.vals = validators.Numbers(min_value=-3.1e-3, max_value=3.1e-3)
    dac.ramping_enabled(True)
    dac.ramp_rate(40e-6)
    dac.ramp_max_step(1e-6)
    return dac


def _get_spi_module(
    spi_rack: SpiRack, coupler_map_entry: CouplerMapEntry, is_dummy: bool = False
) -> InstrumentModule | str:
    """Gets the SPI comodule for the given coupler map entry

    Args:
        spi_rack: SpiRack instance
        coupler_map_entry: Coupler mapping entry from the metadata
        is_dummy: Whether the SPI Rack module should be initialized as a dummy SPI rack module

    Returns:
           the initialized SPI module or just a string if it is dummy
    """
    spi_mod_number = coupler_map_entry.spi_module_number
    dac_name = coupler_map_entry.dac_name
    module_name = f"module{spi_mod_number}"

    if is_dummy:
        return f"Dummy_DAC_for_{module_name}_{dac_name}"

    return spi_rack.instrument_modules[module_name].instrument_modules[dac_name]
