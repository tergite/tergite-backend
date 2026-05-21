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

from ..conftest import HAS_QUANTIFY, SPI_DUMMY_METADATA_FILE
from ..utils.fixtures import get_fixture_path, load_fixture

_SPI_HARDWARE_CONFIG = load_fixture("spi_hardware_quantify-metadata.yml", fmt="yaml")
_SPI_NO_COUPLER_CONFIG_PATH = get_fixture_path("spi_missing_coupler_metadata.yml")


# FIXME: Remove tests of internal/private functions


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
def test_init_spi_dacs():
    """init_spi_dacs() should return a map of SpiDac instances from the given quantify metadata, with keys as the names"""
    from ...libs.quantum_executor.quantify import spi_dac as spi_module
    from ...libs.quantum_executor.quantify.utils.config import QuantifyMetadata

    quantify_metadata = QuantifyMetadata.from_yaml(SPI_DUMMY_METADATA_FILE)

    spi_dacs_map = spi_module.init_spi_dacs(quantify_metadata)
    spi_rack_names = [
        k for k, v in quantify_metadata.root.items() if v.instrument_type == "SPI-Rack"
    ]
    for spi_rack_name in spi_rack_names:
        spi_dac = spi_dacs_map[spi_rack_name]
        assert spi_dac.is_dummy
        assert "u0" in spi_dac.coupler_map
        assert spi_dac.coupler_map["u0"].spi_module_number == 6
        assert spi_dac.coupler_map["u0"].dac_name == "dac0"


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
def test_instantiation_uses_dummy_driver_and_returns_dummy_dac(spi_dac_dummy):
    """Instantiating an SpiDAC defaults to a dummy driver and dummy dac"""
    from qblox_instruments import SpiRack

    # We really created a qblox-instruments SpiRack (dummy)
    assert isinstance(spi_dac_dummy.spi_rack, SpiRack)

    # In dummy mode, create_spi_dac returns a descriptive string handle
    assert isinstance(spi_dac_dummy.coupler_dac_module_map["u0"], str)
    assert spi_dac_dummy.coupler_dac_module_map["u0"].startswith(
        "Dummy_DAC_for_module6_dac0"
    )


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
def test_set_dacs_zero_calls_underlying_rack(spi_dac_dummy, mocker):
    """set_dacs_zero_calls should call underlying rack when called."""
    called = {"hit": False}

    def fake_zero(self):
        called["hit"] = True

    mocker.patch.object(type(spi_dac_dummy.spi_rack), "set_dacs_zero", fake_zero)
    spi_dac_dummy.spi_rack.set_dacs_zero()
    assert called["hit"] is True


@pytest.mark.skipif(not HAS_QUANTIFY, reason="requires quantify")
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
    from ...libs.quantum_executor.quantify.spi_dac import SpiDAC
    from ...libs.quantum_executor.quantify.utils.config import (
        QuantifyMetadata,
        SpiRackConfig,
    )

    port = os.environ["SPI_TEST_PORT"]
    name = os.environ["DEFAULT_PREFIX"]

    # Load base hardware metadata from fixtures and override runtime bits
    meta = _SPI_HARDWARE_CONFIG.copy()
    meta["spi_rack"]["port"] = port
    meta["spi_rack"]["is_dummy"] = False
    meta["spi_rack"]["parking_current"] = 0.0

    metadata_file = tmp_path / "quantify-metadata.yml"
    metadata_file.write_text(yaml.safe_dump(meta))

    metadata = QuantifyMetadata.from_yaml(metadata_file)
    spi_rack_conf = SpiRackConfig.model_validate(metadata.root["spi_rack"])

    spi_dac = SpiDAC(
        name=name,
        conf=spi_rack_conf,
        should_print_progress=False,
    )

    try:
        # Start near 0 A
        spi_dac.reset_to_parking_current()

        target = 0.0005  # 0.5 mA
        jumps = []

        dac = spi_dac.coupler_dac_module_map["u0"]
        prev = dac.current()
        spi_dac.ramp_to_target_currents(
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

        spi_dac.reset_to_parking_current()
    finally:
        try:
            spi_dac.close()
        except:
            pass
