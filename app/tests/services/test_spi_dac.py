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

import os
import time

import pytest
import yaml
from qblox_instruments import SpiRack

from ...libs.quantum_executor.quantify import spi_dac as spi_module
from ...libs.quantum_executor.quantify.spi_dac import SpiDAC
from ..conftest import SPI_QUANTIFY_METADATA_FILE
from ..utils.fixtures import get_fixture_path, load_fixture

_SPI_HARDWARE_CONFIG = load_fixture("spi_hardware_quantify-metadata.yml", fmt="yaml")
_SPI_NO_COUPLER_CONFIG_PATH = get_fixture_path("spi_missing_coupler_metadata.yml")


def test_metadata_parsing_and_create_dummy_dac():
    port, is_dummy, mapping = spi_module._get_spi_metadata(SPI_QUANTIFY_METADATA_FILE)
    assert is_dummy is True
    assert "u0" in mapping
    assert mapping["u0"].spi_module_number == 6
    assert mapping["u0"].dac_name == "dac0"

    # On POSIX, non-existent device should fail validation (only when used)
    if os.name != "nt":
        assert spi_module._find_and_validate_spi_port("/dev/THIS_IS_NOT_THERE") is None


def test_instantiation_uses_dummy_driver_and_returns_dummy_dac(spi_dac_dummy):
    """Instantiating an SpiDAC defaults to a dummy driver and dummy dac"""
    # We really created a qblox-instruments SpiRack (dummy)
    assert isinstance(spi_dac_dummy.spi, SpiRack)

    # In dummy mode, create_spi_dac returns a descriptive string handle
    assert isinstance(spi_dac_dummy.dacs_dictionary["u0"], str)
    assert spi_dac_dummy.dacs_dictionary["u0"].startswith("Dummy_DAC_for_module6_dac0")


def test_set_parking_requires_value_in_redis(spi_dac_dummy):
    """No Redis value → should raise with clear message"""
    with pytest.raises(ValueError) as ei:
        spi_dac_dummy.set_parking_currents(["u0"])
    assert "parking current is not present on redis" in str(ei.value)


def test_missing_coupler_in_metadata_raises_keyerror(redis_client):
    """
    Use a dedicated fixture file missing 'u1' mapping to assert clear error.
    """
    missing_path = _SPI_NO_COUPLER_CONFIG_PATH
    name = os.environ["DEFAULT_PREFIX"]

    with pytest.raises(KeyError) as ei:
        SpiDAC(
            couplers=["u1"],
            metadata_path=missing_path,
            connection=redis_client,
            name=name,
        )

    assert "Coupler 'u1' missing in metadata.yml under 'coupler_spi_mapping'." in str(
        ei.value
    )


def test_set_dacs_zero_calls_underlying_rack(spi_dac_dummy, mocker):
    """set_dacs_zero_calls should call underlying rack when called."""
    called = {"hit": False}

    def fake_zero(self):
        called["hit"] = True

    mocker.patch.object(type(spi_dac_dummy.spi), "set_dacs_zero", fake_zero)
    spi_dac_dummy.set_dacs_zero()
    assert called["hit"] is True


@pytest.mark.skipif(
    not os.environ.get("SPI_TEST_PORT"),
    reason="Set SPI_TEST_PORT=/dev/ttyXXX (or COMX) to run hardware tests.",
)
def test_ramp_behavior_on_real_rack(tmp_path, redis_client):
    """
    Hardware test (opt-in via SPI_TEST_PORT):
      - No per-step jump > 1 µA.
      - Final value within 5 µA of target.
      - Duration in a reasonable envelope for the default ramp rate.
    """

    port = os.environ["SPI_TEST_PORT"]
    name = os.environ["DEFAULT_PREFIX"]

    # Load base hardware metadata from fixtures and override runtime bits
    meta = _SPI_HARDWARE_CONFIG.copy()
    meta["spi_rack"]["port"] = port
    meta["spi_rack"]["is_dummy"] = False

    metadata_file = tmp_path / "quantify-metadata.yml"
    metadata_file.write_text(yaml.safe_dump(meta))

    try:
        sd = SpiDAC(
            couplers=["u0"],
            metadata_path=str(metadata_file),
            print_progress=False,
            name=name,
            connection=redis_client,
        )

        # Start near 0 A
        redis_client.hset("couplers:u0", "parking_current", 0.0)
        sd.set_parking_currents(["u0"])

        target = 0.0005  # 0.5 mA
        jumps = []

        dac = sd.dacs_dictionary["u0"]
        prev = dac.current()
        sd.set_dac_current(
            {"u0": target}
        )  # real rack -> triggers ramp_current_serially

        t0 = time.time()
        while dac.is_ramping():
            cur = dac.current()
            jumps.append(abs(cur - prev))
            prev = cur
            time.sleep(0.05)
        t1 = time.time()

        final = dac.current()
        assert max(jumps) <= 1e-6 + 1e-9  # ≤ 1 µA per step
        assert abs(final - target) <= 5e-6  # ≤ 5 µA at the end
        assert 3.0 <= (t1 - t0) <= 30.0  # envelope for default ramp rate

        sd.set_parking_currents(["u0"])
        sd.close_spi_rack()
    finally:
        try:
            sd.close_spi_rack()
        except:
            pass
