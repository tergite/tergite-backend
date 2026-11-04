# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Tests for the device parameters"""

from datetime import datetime, timezone
from typing import Any, Dict

import pytest
import time_machine
import toml

from libs.device_parameters import BackendConfig, Device, DeviceCalibration
from libs.device_parameters.dtos import (
    ChipConfig,
    SimulatorConfig,
    _BackendGeneralConfig,
)
from tests.utils.chips import get_chip_config
from tests.utils.fixtures import get_fixture_path


def test_online_date_defaults_to_when_the_config_is_made():
    with time_machine.travel(datetime(2030, 1, 1, tzinfo=timezone.utc), tick=False):
        config = _BackendGeneralConfig(
            name="loke", num_qubits=1, num_couplers=0, num_resonators=1, dt=1, dtm=1
        )

    assert config.online_date.startswith("2030-01-01")


@pytest.mark.parametrize(
    "backend_config_file, seed_file",
    [
        ("backend_config.simq2.toml", "sim_2q.seed.toml"),
        ("backend_config.toml", "dummy_quantify.seed.toml"),
    ],
)
def test_couplers_of_a_config_take_their_qubits_from_its_coupling_dict(
    backend_config_file, seed_file
):
    """So that the calibration published at start names them, and can be a seed again"""
    config = BackendConfig.from_toml(
        get_fixture_path(backend_config_file), get_fixture_path(seed_file)
    )

    calibration = DeviceCalibration.from_config(config)

    for coupler in calibration.couplers:
        assert sorted([coupler.control_qubit, coupler.target_qubit]) == sorted(
            config.device_config.coupling_dict[coupler.short_name]
        )
    assert calibration.to_calibration_config().coupler


def test_a_coupler_missing_from_the_coupling_dict_is_refused(tmp_path):
    """A seed's coupler without a name takes its qubits from the coupling dict"""
    seed = toml.load(get_fixture_path("sim_2q.seed.toml"))
    for coupler in seed["calibration_config"]["coupler"]:
        for field in ("full_name", "control_qubit", "target_qubit"):
            coupler.pop(field, None)
        coupler["id"] = "u0"
    seed_file = tmp_path / "seed.toml"
    seed_file.write_text(toml.dumps(seed))
    config = BackendConfig.from_toml(
        get_fixture_path("backend_config.simq2.toml"), seed_file
    )
    config = config.model_copy(
        update={
            "device_config": config.device_config.model_copy(
                update={"coupling_dict": {}}
            )
        }
    )

    with pytest.raises(ValueError, match="u0"):
        DeviceCalibration.from_config(config)


def test_qubits_out_of_the_order_of_their_numbers_are_refused(tmp_path):
    """A circuit's qubit i is the i-th by number, and the coupling map MSS gets indexes
    qubits by their place in qubit_ids, so a config whose two orders differ is refused
    """
    settings = get_fixture_path("backend_config.simq2.toml")
    swapped = tmp_path / "backend_config.toml"
    swapped.write_text(
        open(settings)
        .read()
        .replace('qubit_ids = [ "q12", "q13" ]', 'qubit_ids = [ "q13", "q12" ]')
    )

    with pytest.raises(ValueError, match="not in the order of their numbers"):
        BackendConfig.from_toml(swapped, get_fixture_path("sim_2q.seed.toml"))


def test_device_sends_each_config_value_once_under_its_own_name():
    """MSS keeps only the fields it declares, and on SQL refuses a device with any other"""
    config = BackendConfig.from_toml(
        get_fixture_path("backend_config.simq2.toml"),
        get_fixture_path("sim_2q.seed.toml"),
    )
    general = config.general_config

    got = Device.from_config(config).model_dump()

    assert set(got) == set(Device.model_fields)
    assert (
        got["number_of_qubits"],
        got["number_of_couplers"],
        got["number_of_resonators"],
        got["is_simulator"],
        got["last_online"],
    ) == (
        general.num_qubits,
        general.num_couplers,
        general.num_resonators,
        general.simulator,
        general.online_date,
    )


@pytest.mark.parametrize(
    "settings, seed, expected_cz",
    [
        ("backend_config.simq1.toml", "sim_1q.seed.toml", []),
        ("backend_config.simq2.toml", "sim_2q.seed.toml", ["cz"]),
    ],
)
def test_device_lists_the_gates_gate_level_jobs_compile(settings, seed, expected_cz):
    """basis_gates are what the SDK transpiles to, not the config's gate settings"""
    config = BackendConfig.from_toml(get_fixture_path(settings), get_fixture_path(seed))

    got = Device.from_config(config)

    assert got.basis_gates == [
        "rz",
        "sx",
        "x",
        "rx",
        "measure",
        "reset",
        "delay",
        *expected_cz,
    ]


def test_a_simulator_section_takes_a_chip():
    config = BackendConfig.from_toml(
        get_fixture_path("backend_config.simq2.toml"),
        get_fixture_path("dummy_quantify.seed.toml"),
    )

    assert sorted(config.simulator.chip.qubits) == ["q12", "q13"]
    assert config.simulator.chip.couplers["q12_q13"].couplings == {
        "q12": 0.1e9,
        "q13": 0.1e9,
    }


@pytest.mark.parametrize(
    "change, error",
    [
        ({"qubits": {"q12": {"t2": 2e-4}}}, "longer than twice t1"),
        ({"resonators": {"q13": None}}, r"qubits \['q13'\] have no resonator"),
        ({"couplers": {"q12_q13": {"couplings": {"q13": None}}}}, "two qubits"),
        (
            {"couplers": {"q12_q13": {"couplings": {"q13": None, "q14": 1e8}}}},
            "not named for the qubits it couples",
        ),
        (
            {
                "couplers": {
                    "q12_q15": {
                        "max_frequency": 7e9,
                        "anharmonicity": -1e8,
                        "flux_per_amplitude": 0.1,
                        "couplings": {"q12": 1e8, "q15": 1e8},
                    }
                }
            },
            r"couples qubits \['q15'\] the chip lacks",
        ),
    ],
)
def test_an_impossible_chip_is_refused(change, error):
    chip = _merge(get_chip_config().model_dump(), change)

    with pytest.raises(ValueError, match=error):
        ChipConfig.model_validate(chip)


def _merge(base: Dict[str, Any], change: Dict[str, Any]) -> Dict[str, Any]:
    """Gets the base with the change's values over its own, a None removing a key"""
    merged = dict(base)
    for key, value in change.items():
        if value is None:
            merged.pop(key)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _merge(base[key], value)
        else:
            merged[key] = value
    return merged


def test_the_qutip_simulators_readout_points_are_refused():
    with pytest.raises(ValueError, match="takes a chip, its readout_iq"):
        SimulatorConfig.model_validate(
            {"readout_iq": {"q12": [[1, 0], [-1, 0]]}, "readout_noise": 0.1}
        )
