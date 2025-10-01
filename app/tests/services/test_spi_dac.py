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
import pytest
import time
import yaml

from pathlib import Path

from qblox_instruments import SpiRack

# SUT
from ...libs.quantum_executor.quantify import spi_dac as spi_module
from ...libs.quantum_executor.quantify.spi_dac import SpiDAC

# Shared tests utils
from app.tests.utils.fixtures import get_fixture_path, load_fixture


@pytest.fixture
def spi_metadata_path() -> str:
    return get_fixture_path("spi_dummy_quantify-metadata.yml")


@pytest.fixture
def patched_settings(mocker, spi_metadata_path, redis_client):
    """
    Patch exactly what SpiDAC reads from `settings`:
    - DEFAULT_PREFIX
    - REDIS_CONNECTION
    - QUANTIFY_METADATA_FILE
    """
    mocker.patch.object(spi_module.settings, "DEFAULT_PREFIX", "quantify", create=True)
    mocker.patch.object(
        spi_module.settings, "REDIS_CONNECTION", redis_client, create=True
    )
    mocker.patch.object(
        spi_module.settings, "QUANTIFY_METADATA_FILE", spi_metadata_path, create=True
    )
    return spi_module.settings


@pytest.fixture
def spi_dac_dummy(patched_settings) -> SpiDAC:
    """
    Construct SpiDAC bound to the dummy SPI-Rack.
    """
    sd = SpiDAC(couplers=["u0"], metadata_path=patched_settings.QUANTIFY_METADATA_FILE)
    try:
        yield sd
    finally:
        try:
            sd.close_spi_rack()
        except Exception:
            pass


def test_metadata_parsing_and_create_dummy_dac(spi_metadata_path):
    port, is_dummy, mapping = spi_module._get_spi_metadata(spi_metadata_path)
    assert is_dummy is True
    assert "u0" in mapping
    assert mapping["u0"].spi_module_number == 6
    assert mapping["u0"].dac_name == "dac0"

    # On POSIX, non-existent device should fail validation (only when used)
    if os.name != "nt":
        assert spi_module._find_and_validate_spi_port("/dev/THIS_IS_NOT_THERE") is None


def test_instantiation_uses_dummy_driver_and_returns_dummy_dac(
    patched_settings,
):
    sd = SpiDAC(couplers=["u0"], metadata_path=patched_settings.QUANTIFY_METADATA_FILE)

    # We really created a qblox-instruments SpiRack (dummy)
    assert isinstance(sd.spi, SpiRack)

    # In dummy mode, create_spi_dac returns a descriptive string handle
    assert isinstance(sd.dacs_dictionary["u0"], str)
    assert sd.dacs_dictionary["u0"].startswith("Dummy_DAC_for_module6_dac0")

    sd.close_spi_rack()


def test_set_parking_requires_value_in_redis(spi_dac_dummy):
    # No Redis value → should raise with clear message
    with pytest.raises(ValueError) as ei:
        spi_dac_dummy.set_parking_currents(["u0"])
    assert "parking current is not present on redis" in str(ei.value)


def test_missing_coupler_in_metadata_raises_keyerror(mocker, redis_client):
    """
    Use a dedicated fixture file missing 'u1' mapping to assert clear error.
    """
    missing_path = get_fixture_path("spi_missing_coupler_metadata.yml")

    # Patch settings to point at this fixture + real test redis
    mocker.patch.object(spi_module.settings, "DEFAULT_PREFIX", "quantify", create=True)
    mocker.patch.object(
        spi_module.settings, "REDIS_CONNECTION", redis_client, create=True
    )
    mocker.patch.object(
        spi_module.settings, "QUANTIFY_METADATA_FILE", missing_path, create=True
    )

    with pytest.raises(KeyError) as ei:
        SpiDAC(couplers=["u1"], metadata_path=missing_path)

    assert "Coupler 'u1' missing in metadata.yml under 'coupler_spi_mapping'." in str(
        ei.value
    )


def test_set_dacs_zero_calls_underlying_rack(spi_dac_dummy, mocker):
    called = {"hit": False}

    def fake_zero(self):
        called["hit"] = True

    # mocker.patch.object(spi_dac_dummy.spi, "set_dacs_zero", fake_zero)
    mocker.patch.object(type(spi_dac_dummy.spi), "set_dacs_zero", fake_zero)
    spi_dac_dummy.set_dacs_zero()
    assert called["hit"] is True


@pytest.mark.skipif(
    not os.environ.get("SPI_TEST_PORT"),
    reason="Set SPI_TEST_PORT=/dev/ttyXXX (or COMX) to run hardware tests.",
)
def test_ramp_behavior_on_real_rack(mocker, tmp_path, redis_client):
    """
    Hardware test (opt-in via SPI_TEST_PORT):
      - No per-step jump > 1 µA.
      - Final value within 5 µA of target.
      - Duration in a reasonable envelope for the default ramp rate.
    """

    port = os.environ["SPI_TEST_PORT"]

    # Load base hardware metadata from fixtures and override runtime bits
    meta = load_fixture("spi_hardware_quantify-metadata.yml", fmt="yaml")
    meta["spi_rack"]["port"] = port
    meta["spi_rack"]["is_dummy"] = False

    p = tmp_path / "quantify-metadata.yml"
    p.write_text(yaml.safe_dump(meta))

    # Patch settings for hardware path + real test redis
    mocker.patch.object(spi_module.settings, "DEFAULT_PREFIX", "quantify", create=True)
    mocker.patch.object(
        spi_module.settings, "REDIS_CONNECTION", redis_client, create=True
    )
    mocker.patch.object(
        spi_module.settings, "QUANTIFY_METADATA_FILE", str(p), create=True
    )

    sd = SpiDAC(couplers=["u0"], metadata_path=str(p), print_progress=False)

    # Start near 0 A
    redis_client.hset("couplers:u0", "parking_current", 0.0)
    sd.set_parking_currents(["u0"])

    target = 0.0005  # 0.5 mA
    jumps = []

    dac = sd.dacs_dictionary["u0"]
    prev = dac.current()
    sd.set_dac_current({"u0": target})  # real rack -> triggers ramp_current_serially

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
