import os
import sys
import types
import yaml
import pytest
from pathlib import Path

qblox = pytest.importorskip("qblox_instruments")

# Import module + class under test
from ...libs.quantum_executor.quantify import spi_dac as spi_module
from ...libs.quantum_executor.quantify.spi_dac import SpiDAC


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
    """
    Write a minimal quantify-metadata.yml that includes a dummy Cluster (to satisfy schema)
    and a dummy SPI-Rack with one coupler mapping (u0 -> module6/dac0).
    """
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
    """
    Provide a minimal settings object compatible with SpiDAC.
    """
    st = types.SimpleNamespace()
    st.DEFAULT_PREFIX = "quantify"
    st.REDIS_CONNECTION = FakeRedis()
    st.QUANTIFY_METADATA_FILE = str(tmp_metadata_path)
    return st


@pytest.fixture
def patch_settings(monkeypatch: pytest.MonkeyPatch, settings_stub):
    """
    Patch the 'settings' module that spi_dac imports, so SpiDAC uses
    our in-memory redis + temp metadata.
    """
    monkeypatch.setattr(spi_module, "settings", settings_stub, raising=False)
    return settings_stub


@pytest.fixture
def spi_dac_dummy(patch_settings) -> SpiDAC:
    """
    Build SpiDAC bound to the dummy SPI-Rack from metadata (is_dummy=True).
    """
    sd = SpiDAC(couplers=["u0"], metadata_path=patch_settings.QUANTIFY_METADATA_FILE)
    yield sd
    # Ensure close even if a test failed midway
    try:
        sd.close_spi_rack()
    except Exception:
        pass


def test_metadata_parsing_and_create_dummy_dac(tmp_metadata_path):
    port, is_dummy, mapping = spi_module._get_spi_metadata(tmp_metadata_path)
    assert is_dummy is True
    assert "u0" in mapping
    assert mapping["u0"].spi_module_number == 6
    assert mapping["u0"].dac_name == "dac0"

    # On POSIX, non-existent device should fail validation (only when used)
    if os.name != "nt":
        assert spi_module._find_and_validate_spi_port("/dev/THIS_IS_NOT_THERE") is None


def test_instantiation_uses_dummy_driver_and_returns_dummy_dac(
    monkeypatch, patch_settings
):
    sd = SpiDAC(couplers=["u0"], metadata_path=patch_settings.QUANTIFY_METADATA_FILE)

    # We really created a qblox-instruments SpiRack (dummy)
    assert isinstance(sd.spi, qblox.SpiRack)

    # In dummy mode, create_spi_dac returns a descriptive string handle
    assert isinstance(sd.dacs_dictionary["u0"], str)
    assert sd.dacs_dictionary["u0"].startswith("Dummy_DAC_for_module6_dac0")

    # Close without errors
    sd.close_spi_rack()


def test_set_parking_requires_value_in_redis(spi_dac_dummy):
    # No Redis value → should raise with clear message
    with pytest.raises(ValueError) as ei:
        spi_dac_dummy.set_parking_currents(["u0"])
    assert "parking current is not present on redis" in str(ei.value)


def test_missing_coupler_in_metadata_raises_keyerror(tmp_path: Path, monkeypatch):
    # Build metadata without u1 mapping
    meta = {
        "spi_rack": {
            "instrument_type": "SPI-Rack",
            "port": "/dev/ttyACM0",
            "is_dummy": True,
            "coupler_spi_mapping": {
                "u0": {"spi_module_number": 6, "dac_name": "dac0"},
            },
        },
        "cluster0": {
            "instrument_type": "Cluster",
            "ip_address": "192.168.0.101",
            "is_dummy": True,
            "modules": {"2": {"instrument_type": "QCM_RF"}},
        },
    }
    p = tmp_path / "metadata.yml"
    p.write_text(yaml.safe_dump(meta))

    # Patch settings to point to this metadata
    settings_stub = types.SimpleNamespace(
        DEFAULT_PREFIX="quantify",
        REDIS_CONNECTION=FakeRedis(),
        QUANTIFY_METADATA_FILE=str(p),
    )
    monkeypatch.setattr(spi_module, "settings", settings_stub, raising=False)

    with pytest.raises(KeyError) as ei:
        SpiDAC(couplers=["u1"], metadata_path=str(p))

    assert "Coupler 'u1' missing in metadata.yml under 'coupler_spi_mapping'." in str(
        ei.value
    )


def test_set_dacs_zero_calls_underlying_rack(spi_dac_dummy, monkeypatch):
    called = {"hit": False}

    def fake_zero():
        called["hit"] = True

    monkeypatch.setattr(spi_dac_dummy.spi, "set_dacs_zero", fake_zero, raising=False)
    spi_dac_dummy.set_dacs_zero()
    assert called["hit"] is True


@pytest.mark.skipif(
    not os.environ.get("SPI_TEST_PORT"),
    reason="Set SPI_TEST_PORT=/dev/ttyXXX (or COMX) to run hardware tests.",
)
def test_ramp_behavior_on_real_rack(monkeypatch, tmp_path: Path):
    """
    This test exercises the true ramping logic against a *real* SPI rack.

    Usage:
        export SPI_TEST_PORT=/dev/ttyACM0  # or COM3
        pytest -m hardware -k ramp_behavior_on_real_rack

    What it checks:
        - No per-step jump > ramp_max_step (1 µA).
        - Final value reached within a 5 µA tolerance.
        - Total time within a reasonable envelope (based on ramp_rate).
    """
    port = os.environ["SPI_TEST_PORT"]
    meta = {
        "spi_rack": {
            "instrument_type": "SPI-Rack",
            "port": port,
            "is_dummy": False,
            "coupler_spi_mapping": {
                "u0": {"spi_module_number": 6, "dac_name": "dac0"},
            },
        },
        "cluster0": {
            "instrument_type": "Cluster",
            "ip_address": "192.168.0.101",
        },
    }
    p = tmp_path / "metadata.yml"
    p.write_text(yaml.safe_dump(meta))

    settings_stub = types.SimpleNamespace(
        DEFAULT_PREFIX="quantify",
        REDIS_CONNECTION=FakeRedis(),
        QUANTIFY_METADATA_FILE=str(p),
    )
    monkeypatch.setattr(spi_module, "settings", settings_stub, raising=False)

    sd = SpiDAC(couplers=["u0"], metadata_path=str(p), print_progress=False)

    # Start near 0 A
    settings_stub.REDIS_CONNECTION.hset("couplers:u0", "parking_current", 0.0)
    sd.set_parking_currents(["u0"])

    target = 0.0005  # 0.5 mA
    # Track successive reads to verify per-step jump
    jumps = []

    # Wrap the DAC to sample values during ramp
    dac = sd.dacs_dictionary["u0"]
    prev = dac.current()
    sd.set_dac_current({"u0": target})  # real rack -> triggers ramp_current_serially

    # Poll until close enough
    import time

    t0 = time.time()
    while dac.is_ramping():
        cur = dac.current()
        jumps.append(abs(cur - prev))
        prev = cur
        time.sleep(0.05)
    t1 = time.time()

    final = dac.current()
    # 1 µA max step
    assert max(jumps) <= 1e-6 + 1e-9
    # within 5 µA at the end
    assert abs(final - target) <= 5e-6

    # Time envelope: ramp_rate=40 µA/s → 0.5 mA takes ~12.5 s; give a safety band (3..30s)
    elapsed = t1 - t0
    assert 3.0 <= elapsed <= 30.0

    # Reset and close
    sd.set_parking_currents(["u0"])
    sd.close_spi_rack()
