# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import logging

import pytest

from ...libs.quantum_executor.quantify.spi_dac import SpiDAC
from ..conftest import TEST_SPI_LOGGER_NAME


def test_verbose_happy_path(caplog, verbose_spi_dac_dummy, mocker, redis_client):
    """
    Log-heavy check of the nominal flow on dummy rack:
    - parking -> bias -> back to parking -> close
    - ensure dummy path *does not* call ramp_current_serially
    - ensure expected module logs are present
    """
    spi_dac_dummy = verbose_spi_dac_dummy
    testlog = logging.getLogger(TEST_SPI_LOGGER_NAME)
    caplog.set_level(logging.DEBUG)

    # Spy: catch accidental ramping even in dummy mode
    called_ramp = {"count": 0}
    orig_ramp = _get_orig_ramp_current()
    assert orig_ramp is not None, "Failed to recover original ramp_to_target_currents"

    def ramp_spy(self, values):
        called_ramp["count"] += 1
        testlog.error(
            "!!! ramp_to_target_currents CALLED in dummy mode with %r", values
        )
        return orig_ramp(self, values)

    mocker.patch.object(SpiDAC, "ramp_to_target_currents", ramp_spy)

    testlog.info("=== SET TO PARKING ===")
    spi_dac_dummy.reset_to_parking_current()

    testlog.info("=== SET TO BIAS 2 mA ===")
    spi_dac_dummy.ramp_to_target_currents({"u0": 2e-3})

    testlog.info("=== BACK TO PARKING ===")
    spi_dac_dummy.reset_to_parking_current()

    # Close & verify we really call underlying close()
    closed = {"hit": False}
    orig_close = spi_dac_dummy.spi_rack.close

    def close_probe():
        closed["hit"] = True
        testlog.debug("close() invoked on SpiRack")
        return orig_close()

    setattr(spi_dac_dummy.spi_rack, "close", close_probe)

    testlog.info("=== CLOSE RACK ===")
    spi_dac_dummy.close()

    # Assertions guided by logs
    dummy_lines = [
        rec
        for rec in caplog.records
        if rec.name.startswith("app.libs.quantum_executor.quantify.spi_dac")
        and "Dummy DAC to current" in rec.getMessage()
    ]
    assert len(dummy_lines) >= 2, "Expected dummy current set logs to appear"

    entries = [
        r
        for r in caplog.records
        if r.name == TEST_SPI_LOGGER_NAME
        and r.levelno == logging.DEBUG
        and r.getMessage().startswith("ENTER")
    ]
    exits = [
        r
        for r in caplog.records
        if r.name == TEST_SPI_LOGGER_NAME
        and r.levelno == logging.DEBUG
        and r.getMessage().startswith("EXIT")
    ]
    assert any("reset_to_parking_current" in r.getMessage() for r in entries)
    assert any("close" in r.getMessage() for r in entries)
    assert len(exits) >= len(entries) - 1

    assert closed["hit"] is True
    assert not any(
        "Ramping finished" in r.getMessage() for r in caplog.records
    ), "Should not log 'Ramping finished' in dummy mode"


def _get_orig_ramp_current():
    """
    Gets the original ramp current from the ramp_to_target_currents.
    """
    orig = getattr(SpiDAC, "__orig_ramp_to_target_currents", None)
    if orig is None:
        # fallback if wrappers changed
        wrapped = getattr(SpiDAC, "ramp_to_target_currents", None)
        if wrapped is not None and hasattr(wrapped, "__wrapped__"):
            orig = wrapped.__wrapped__
    return orig
