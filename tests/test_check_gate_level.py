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
"""Tests of dev/check_gate_level.py, which checks a device's configs for gate-level jobs"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import toml

from libs.device_parameters import BackendConfig
from tests.utils.circuits import get_gate_level_seed
from tests.utils.env import (
    TEST_BACKEND_SETTINGS_FILE,
    TEST_QUANTIFY_CONFIG_FILE,
    TEST_QUANTIFY_SEED_FILE,
    TEST_SIM_2Q_SEED_FILE,
    TEST_SIMQ2_BACKEND_SETTINGS_FILE,
)


def test_configs_gate_level_jobs_run_on_have_no_errors(tmp_path):
    """The seed the gate-level tests run on has no errors, and its pair whose coupler has
    no calibration is warned of, as it gets no cz"""
    seed = get_gate_level_seed()
    calibration = seed["calibration_config"]
    calibration["coupler"] = calibration["coupler"][:1]

    findings = _check(tmp_path, seed)

    assert [f for f in findings if f.is_error] == []
    assert [(f.subject, f.message) for f in findings] == [
        ("q12_q14", "has no calibration, so its qubits get no cz")
    ]


def test_lo_beyond_the_nco_from_the_frequency_names_the_lo_to_set(tmp_path):
    """A port and clock whose LO is more than an NCO's 500 MHz from its frequency, as the
    test seed's q13 is from its drive's, is an error naming the LO to set"""
    findings = _check(tmp_path, toml.load(TEST_QUANTIFY_SEED_FILE))

    assert _get_messages(findings, "q13")[0] == (
        "the LO of q13:mw-q13.01 is 5306.0 MHz, so its NCO would be -628.9 MHz, "
        "beyond ±500 MHz; set the LO within 500 MHz of its 4677.1 MHz"
    )


def test_coupler_calibrated_with_no_cz_pulse_is_an_error(tmp_path):
    """A coupler whose calibration has no CZ pulse, as an uncalibrated one, is an error, as
    the SDK still offers cz on its qubits"""
    findings = _check(tmp_path, get_gate_level_seed())

    assert _get_messages(findings, "q12_q14") == [
        "is calibrated with no CZ pulse, so the cz the SDK offers on its qubits only "
        "shifts their phases"
    ]


def test_qubit_without_its_lda_is_an_error(tmp_path):
    """A qubit with no linear discriminator is an error, as its counts cannot be got"""
    seed = get_gate_level_seed()
    del seed["calibration_config"]["discriminators"]["lda"]["q12"]["intercept"]

    findings = _check(tmp_path, seed)

    assert _get_messages(findings, "q12") == [
        "has no intercept in discriminators.lda.q12, so its counts (meas_level 2) "
        "cannot be discriminated"
    ]


def test_qubit_without_its_pi_pulse_amplitude_is_an_error_and_is_not_compiled(
    tmp_path,
):
    """A qubit with no pi pulse amplitude is an error, and its compilation, which would
    only repeat it, is left out"""
    seed = get_gate_level_seed()
    qubit = next(q for q in seed["calibration_config"]["qubit"] if q["id"] == "q12")
    del qubit["pi_pulse_amplitude"]

    findings = _check(tmp_path, seed)

    assert _get_messages(findings, "q12") == [
        "has no pi_pulse_amplitude, which its X gates need"
    ]


def test_port_the_hardware_config_lacks_does_not_compile(tmp_path):
    """A port gate-level jobs play that the hardware config does not connect is an error,
    the compiler's"""
    hardware_config = json.loads(Path(TEST_QUANTIFY_CONFIG_FILE).read_text())
    graph = hardware_config["connectivity"]["graph"]
    graph["nodes"] = [node for node in graph["nodes"] if node["id"] != "q12:mw"]
    graph["links"] = [
        link
        for link in graph["links"]
        if "q12:mw" not in (link["source"], link["target"])
    ]

    findings = _check(tmp_path, get_gate_level_seed(), hardware_config)

    [message] = _get_messages(findings, "q12")
    assert message.startswith("does not compile on q12:mw-q12.01: ")


def test_device_without_pulse_limits_is_warned_of(tmp_path):
    """A device that sets no pulse limits refuses pulse-level jobs, which is no error of
    its gate-level ones"""
    from dev.check_gate_level import Finding, check_configs

    seed = get_gate_level_seed()
    seed["calibration_config"]["coupler"] = seed["calibration_config"]["coupler"][:1]
    seed_file = tmp_path / "seed.toml"
    seed_file.write_text(toml.dumps(seed))
    backend_config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=str(seed_file)
    ).model_copy(update={"pulse_limits": None})

    findings = check_configs(backend_config, _get_hardware_config(None))

    assert (
        Finding(
            False,
            "device",
            "sets no pulse_limits in its backend config, so it refuses pulse-level jobs",
        )
        in findings
    )
    assert [f for f in findings if f.is_error] == []


def test_device_without_calibration_is_an_error():
    """A device with no calibration is an error, as gate-level jobs compile from it"""
    from dev.check_gate_level import Finding, check_configs

    backend_config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=TEST_QUANTIFY_SEED_FILE
    )
    backend_config = backend_config.model_copy(update={"calibration_config": None})

    assert check_configs(backend_config, _get_hardware_config(None)) == [
        Finding(
            True, "device", "has no calibration, which gate-level jobs compile from"
        )
    ]


def test_main_prints_what_it_found_and_exits_1_on_an_error(tmp_path, capsys):
    """main() checks each env file's device, as bcc start reads it, printing each finding,
    and exits 1 as the test seed has errors"""
    from dev.check_gate_level import main

    env_file = tmp_path / "device.env"
    env_file.write_text("BCC_EXECUTOR_TYPE=quantify\n")

    exit_code = main([str(env_file)])

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 1
    assert lines[0].startswith(f"{env_file} (quantify): ")
    assert "  error   q13: the LO of q13:mw-q13.01 is 5306.0 MHz" in "\n".join(lines)


def test_simulated_device_is_not_checked(tmp_path, monkeypatch, capsys):
    from dev.check_gate_level import main

    monkeypatch.setenv("BCC_BACKEND_SETTINGS", TEST_SIMQ2_BACKEND_SETTINGS_FILE)
    monkeypatch.setenv("BCC_CALIBRATION_SEED", TEST_SIM_2Q_SEED_FILE)
    env_file = tmp_path / "device.env"
    env_file.write_text("BCC_EXECUTOR_TYPE=quantify\n")

    exit_code = main([str(env_file)])

    assert exit_code == 0
    assert capsys.readouterr().out == f"{env_file} (quantify): 0 errors, 0 warnings\n"


def _check(
    tmp_path: Path, seed: Dict[str, Any], hardware_config: Optional[dict] = None
) -> List[Any]:
    """Checks the test backend config with the seed, and the hardware config, by default
    the test one"""
    from dev.check_gate_level import check_configs

    seed_file = tmp_path / "seed.toml"
    with open(seed_file, "w") as file:
        toml.dump(seed, file)
    backend_config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=str(seed_file)
    )
    return check_configs(backend_config, _get_hardware_config(hardware_config))


def _get_hardware_config(hardware_config: Optional[dict]) -> Any:
    """The hardware config of the dict, or else the test one"""
    from quantify_scheduler.backends.qblox_backend import (
        QbloxHardwareCompilationConfig,
    )

    from libs.quantum_executor.quantify.utils.config import load_quantify_config

    if hardware_config is None:
        return load_quantify_config(TEST_QUANTIFY_CONFIG_FILE)
    return QbloxHardwareCompilationConfig.model_validate(hardware_config)


def _get_messages(findings: List[Any], subject: str) -> List[str]:
    """The messages of the findings of the subject"""
    return [finding.message for finding in findings if finding.subject == subject]
