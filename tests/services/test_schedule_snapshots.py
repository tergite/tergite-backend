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
"""Tests that random circuits play on each qubit what they played before the refactor

The snapshots are of the schedules the backend at e9f2dea1 compiled from the PulseQobjs
of the SDK of v2026.06.0; dev/schedule_snapshots/generate.sh writes them. That backend
delayed each instruction by a grid interval per instruction before it on its channel, so
what is compared is what each qubit gets, and how long it idles, not exactly when.
"""

import json
from typing import Any, Dict, Generator

import pytest
import toml

from libs.device_parameters import BackendConfig
from libs.quantum_executor.base.circuits import CircuitJobParams, QasmCircuit
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from libs.quantum_executor.quantify.simulated_chip.timeline import (
    Timeline,
    get_timeline,
)
from tests.utils.env import TEST_BACKEND_SETTINGS_FILE, TEST_QUANTIFY_CONFIG_FILE
from tests.utils.fixtures import get_fixture_path
from tests.utils.schedules import (
    get_differences,
    get_idle_time,
    get_qubit_events,
    get_times_of_flight,
    to_timeline,
)

_SEED_FILE = get_fixture_path("schedule_snapshots/device.seed.toml")
with open(get_fixture_path("schedule_snapshots/pre_refactor.json")) as _file:
    _SNAPSHOTS = json.load(_file)
_CIRCUITS = {circuit["name"]: circuit for circuit in _SNAPSHOTS["circuits"]}
# the samples a qubit may idle longer than it did before the refactor, in which it
# decoheres a hundredth of its shortest T2 at most
_IDLE_TOLERANCE = 330
# the samples to the nearest point of the clusters' 4 ns grid, at most
_GRID_HALF = 2


@pytest.fixture(scope="module")
def timelines() -> Generator[Dict[str, Timeline], Any, None]:
    """What the schedule compiled today of each snapshot's circuit plays, by its name"""
    from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

    from libs.quantum_executor.quantify.device import apply_calibration
    from libs.quantum_executor.quantify.utils.config import load_quantify_config

    backend_config = BackendConfig.from_toml(
        TEST_BACKEND_SETTINGS_FILE, seed_file=_SEED_FILE
    )
    device = QuantumDevice("test_schedule_snapshots")
    device.hardware_config(load_quantify_config(TEST_QUANTIFY_CONFIG_FILE))
    apply_calibration(device, backend_config)
    yield {
        name: _get_timeline(device, backend_config, circuit)
        for name, circuit in _CIRCUITS.items()
    }
    device.close()


@pytest.mark.parametrize("name", sorted(_CIRCUITS))
def test_each_qubit_gets_what_it_got_before_the_refactor(name, timelines):
    """The same pulses and acquisitions, in the same order, each the same samples at the
    same frequency and frame phase; a reset, which waited 300 us then and does now,
    plays nothing either way"""
    expected = to_timeline(_CIRCUITS[name]["timeline"], _SNAPSHOTS["waveforms"])

    differences = get_differences(expected, timelines[name])

    assert differences == [], f"{_CIRCUITS[name]['qasm']}\n" + "\n".join(differences)


@pytest.mark.parametrize("name", sorted(_CIRCUITS))
def test_no_qubit_idles_longer_than_before_the_refactor(name, timelines):
    expected = get_qubit_events(
        to_timeline(_CIRCUITS[name]["timeline"], _SNAPSHOTS["waveforms"])
    )
    got = get_qubit_events(timelines[name])

    longer = {}
    for qubit, events in got.items():
        before, now = get_idle_time(expected.get(qubit, [])), get_idle_time(events)
        if now > before + _IDLE_TOLERANCE:
            longer[qubit] = (before, now)

    assert longer == {}, f"{_CIRCUITS[name]['qasm']}\nidle ns, then and now: {longer}"


def test_acquisitions_start_the_calibrated_delay_after_their_readout_pulses(timelines):
    """Before the refactor, they started 2 to 6 ns after it, by how many instructions their
    channel had before them"""
    seed = toml.load(_SEED_FILE)["calibration_config"]
    acq_delays = {
        resonator["id"]: resonator["acq_delay"] * 1e9
        for resonator in seed["readout_resonator"]
    }

    off_delay = {}
    for name, timeline in timelines.items():
        for qubit, events in get_qubit_events(timeline).items():
            times = get_times_of_flight(events)
            if any(abs(time - acq_delays[qubit]) > _GRID_HALF for time in times):
                off_delay[(name, qubit)] = times

    assert off_delay == {}


def _get_timeline(
    device: Any, backend_config: BackendConfig, circuit: Dict[str, Any]
) -> Timeline:
    """Gets what the schedule the device's executor compiles of the snapshot's circuit
    plays"""
    from quantify_scheduler.backends.graph_compilation import SerialCompiler

    from libs.quantum_executor.quantify.experiments import get_circuit_experiments
    from libs.quantum_executor.quantify.scheduling import get_compilation_config

    params = CircuitJobParams(
        circuits=[
            QasmCircuit(
                name=circuit["name"],
                qasm=circuit["qasm"],
                num_clbits=circuit["num_clbits"],
            )
        ],
        shots=circuit["shots"],
    )
    native_config = NativeQobjConfig.from_circuit_job_params(
        params, n_qubits=backend_config.general_config.num_qubits
    )
    compiler = SerialCompiler("test_schedule_snapshots")
    (experiment,) = get_circuit_experiments(
        device, backend_config, compiler, params.circuits, native_config
    )
    compiled = compiler.compile(
        experiment.schedule, config=get_compilation_config(device)
    )
    return get_timeline(compiled)
