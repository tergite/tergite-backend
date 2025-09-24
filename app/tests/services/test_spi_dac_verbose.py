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
import yaml
import time
import types
import logging
import pytest
from functools import wraps
from pathlib import Path

qblox = pytest.importorskip("qblox_instruments")

from ...libs.quantum_executor.quantify import spi_dac as spi_module
from ...libs.quantum_executor.quantify.spi_dac import SpiDAC

TEST_LOGGER_NAME = "test.spi_dac.verbose"
testlog = logging.getLogger(TEST_LOGGER_NAME)
testlog.setLevel(logging.DEBUG)


class FakeRedis:
    def __init__(self):
        self._h = {}

    def hexists(self, key: str, field: str) -> bool:
        return key in self._h and field in self._h[key]

    def hget(self, key: str, field: str):
        return self._h[key][field]

    def hset(self, key: str, field: str, value):
        self._h.setdefault(key, {})[field] = value


@pytest.fixture
def tmp_metadata_path(tmp_path: Path) -> Path:
    meta = {
        "cluster0": {
            "instrument_type": "Cluster",
            "ip_address": "192.168.0.101",
            "is_dummy": True,
            "modules": {"2": {"instrument_type": "QCM_RF"}},
        },
        "spi_rack": {
            "instrument_type": "SPI-Rack",
            "port": "/dev/ttyACM0",
            "is_dummy": True,
            "coupler_spi_mapping": {
                "u0": {"spi_module_number": 6, "dac_name": "dac0"},
            },
        },
    }
    p = tmp_path / "quantify-metadata.yml"
    p.write_text(yaml.safe_dump(meta))
    return p


@pytest.fixture
def settings_stub(tmp_metadata_path: Path):
    st = types.SimpleNamespace()
    st.DEFAULT_PREFIX = "quantify"
    st.REDIS_CONNECTION = FakeRedis()
    st.QUANTIFY_METADATA_FILE = str(tmp_metadata_path)
    return st


@pytest.fixture
def patch_settings(monkeypatch: pytest.MonkeyPatch, settings_stub):
    monkeypatch.setattr(spi_module, "settings", settings_stub, raising=False)
    return settings_stub


@pytest.fixture
def verbose_wrappers(monkeypatch: pytest.MonkeyPatch):
    """
    Wrap core SpiDAC methods to log enter/exit/exception and keep references
    to originals on the class as `__orig_<name>`. Also expose `__wrapped__`
    via functools.wraps so tests can recover the original easily.
    """

    def wrap(name: str):
        orig = getattr(SpiDAC, name)

        @wraps(orig)
        def _wrapped(self, *args, **kwargs):
            testlog.debug(
                "ENTER %s(is_dummy=%s) args=%r kwargs=%r",
                name,
                getattr(self, "is_dummy", None),
                args,
                kwargs,
            )
            try:
                res = orig(self, *args, **kwargs)
                testlog.debug("EXIT  %s -> %r", name, res)
                return res
            except Exception as e:
                testlog.exception("EXC   %s: %s", name, e)
                raise

        # install wrapper and stash the original
        monkeypatch.setattr(SpiDAC, name, _wrapped, raising=True)
        monkeypatch.setattr(SpiDAC, f"__orig_{name}", orig, raising=False)

    for fn in [
        "__init__",
        "create_spi_dac",
        "set_dacs_zero",
        "set_parking_currents",
        "set_dac_current",
        "ramp_current_serially",
        "ramp_current_simultaneusly",
        "print_currents",
        "close_spi_rack",
    ]:
        wrap(fn)


@pytest.fixture
def spi_dac_dummy(patch_settings, verbose_wrappers) -> SpiDAC:
    sd = SpiDAC(couplers=["u0"], metadata_path=patch_settings.QUANTIFY_METADATA_FILE)
    yield sd
    try:
        sd.close_spi_rack()
    except Exception:
        pass


def _get_orig_ramp():
    # recover original function placed by verbose_wrappers
    orig = getattr(SpiDAC, "__orig_ramp_current_serially", None)
    if orig is None:
        # fallback if wrappers changed
        wrapped = getattr(SpiDAC, "ramp_current_serially", None)
        if wrapped is not None and hasattr(wrapped, "__wrapped__"):
            orig = wrapped.__wrapped__
    return orig


def test_verbose_happy_path(caplog, spi_dac_dummy, patch_settings, monkeypatch):
    """
    Log-heavy check of the nominal flow on dummy rack:
    - parking -> bias -> back to parking -> close
    - ensure dummy path *does not* call ramp_current_serially
    - ensure expected module logs are present
    """
    caplog.set_level(logging.DEBUG)

    # Arrange parking
    patch_settings.REDIS_CONNECTION.hset("couplers:u0", "parking_current", 1e-4)

    # Spy: catch accidental ramping even in dummy mode
    called_ramp = {"count": 0}
    orig_ramp = _get_orig_ramp()
    assert orig_ramp is not None, "Failed to recover original ramp_current_serially"

    def ramp_spy(self, values):
        called_ramp["count"] += 1
        testlog.error("!!! ramp_current_serially CALLED in dummy mode with %r", values)
        return orig_ramp(self, values)

    monkeypatch.setattr(SpiDAC, "ramp_current_serially", ramp_spy, raising=True)

    testlog.info("=== SET TO PARKING ===")
    spi_dac_dummy.set_parking_currents(["u0"])

    testlog.info("=== SET TO BIAS 2 mA ===")
    spi_dac_dummy.set_dac_current({"u0": 2e-3})

    testlog.info("=== BACK TO PARKING ===")
    spi_dac_dummy.set_parking_currents(["u0"])

    # Close & verify we really call underlying close()
    closed = {"hit": False}
    orig_close = spi_dac_dummy.spi.close

    def close_probe():
        closed["hit"] = True
        testlog.debug("close() invoked on SpiRack")
        return orig_close()

    setattr(spi_dac_dummy.spi, "close", close_probe)

    testlog.info("=== CLOSE RACK ===")
    spi_dac_dummy.close_spi_rack()

    # Assertions guided by logs
    assert (
        called_ramp["count"] == 0
    ), "ramp_current_serially should not be used in dummy mode"

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
        if r.name == TEST_LOGGER_NAME
        and r.levelno == logging.DEBUG
        and r.getMessage().startswith("ENTER")
    ]
    exits = [
        r
        for r in caplog.records
        if r.name == TEST_LOGGER_NAME
        and r.levelno == logging.DEBUG
        and r.getMessage().startswith("EXIT")
    ]
    assert any("set_parking_currents" in r.getMessage() for r in entries)
    assert any("set_dac_current" in r.getMessage() for r in entries)
    assert any("close_spi_rack" in r.getMessage() for r in entries)
    assert len(exits) >= len(entries) - 1

    assert closed["hit"] is True
    assert not any(
        "Ramping finished" in r.getMessage() for r in caplog.records
    ), "Should not log 'Ramping finished' in dummy mode"


def test_verbose_missing_parking_logs_error_and_raises(caplog, spi_dac_dummy):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError):
        spi_dac_dummy.set_parking_currents(["u0"])
    assert any(
        r.name == TEST_LOGGER_NAME and "EXC   set_parking_currents" in r.getMessage()
        for r in caplog.records
    )
