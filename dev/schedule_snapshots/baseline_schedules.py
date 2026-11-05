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
"""Runs in a checkout of the backend from before the refactor (e9f2dea1, whose packages are
under app/): writes the device info it sent MSS, or snapshots what the schedules it
compiled from the SDK's PulseQobjs play

Usage:
    python baseline_schedules.py device-info <backend_config.toml> <seed.toml> <out.json>
    python baseline_schedules.py schedules <circuits.json> <backend_config.toml>
        <seed.toml> <quantify-config.json> <timeline.py> <snapshots.py> <out.json>

timeline.py and snapshots.py are today's get_timeline and to_snapshot, by their paths.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict

import numpy as np
from app.libs.device_parameters.dtos import BackendConfig, Device, DeviceCalibration
from app.libs.qiskit.qobj import PulseQobj
from app.libs.quantum_executor.base.quantum_job import (
    get_experiment_name,
    to_native_qobj_config,
)
from app.libs.quantum_executor.quantify.executor import (
    _extract_drive_frequencies,
    _extract_lo_frequencies,
)
from app.libs.quantum_executor.quantify.experiment import QuantifyExperiment
from app.libs.quantum_executor.quantify.utils.config import load_quantify_config
from app.libs.quantum_executor.quantify.utils.portclock import generate_hardware_map
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice


def main() -> None:
    """Runs the command of the arguments"""
    command, *args = sys.argv[1:]
    if command == "device-info":
        write_device_info(*args)
    else:
        write_snapshots(*args)


def write_device_info(backend_file: str, seed_file: str, out_file: str) -> None:
    """Writes the device and its calibration, as the backend sent them MSS"""
    config = BackendConfig.from_toml(backend_file, seed_file=seed_file)
    info = {
        "device": json.loads(Device.from_config(config).model_dump_json()),
        "calibration": json.loads(
            DeviceCalibration.from_config(config).model_dump_json()
        ),
    }
    Path(out_file).write_text(json.dumps(info))


def write_snapshots(
    circuits_file: str,
    backend_file: str,
    seed_file: str,
    hardware_file: str,
    timeline_file: str,
    snapshots_file: str,
    out_file: str,
) -> None:
    """Translates each circuit's PulseQobj as the quantify executor did, compiles it and
    writes the snapshots of what the compiled schedules play"""
    get_timeline = _load_module(timeline_file).get_timeline
    to_snapshot = _load_module(snapshots_file).to_snapshot
    backend_config = BackendConfig.from_toml(backend_file, seed_file=seed_file)
    quantify_config = load_quantify_config(hardware_file)
    translation_kwargs = _get_translation_kwargs(backend_config, quantify_config)
    device = QuantumDevice("pre_refactor_device")
    device.hardware_config(quantify_config)
    compiler = SerialCompiler(name="pre_refactor_compiler")
    compilation_config = device.generate_compilation_config()

    waveforms: Dict[str, Any] = {}
    snapshots = []
    for circuit in json.loads(Path(circuits_file).read_text()):
        qobj = PulseQobj.from_dict(circuit.pop("qobj"))
        qobj.config.pulse_library = {
            item.name: np.asarray(item.samples) for item in qobj.config.pulse_library
        }
        (expt,) = qobj.experiments
        experiment = QuantifyExperiment.from_qobj_expt(
            name=get_experiment_name(expt.header.name, 1),
            expt=expt,
            qobj_config=qobj.config,
            native_config=to_native_qobj_config(qobj.config),
            **translation_kwargs,
        )
        compiled = compiler.compile(experiment.schedule, config=compilation_config)
        timeline = to_snapshot(get_timeline(compiled), waveforms)
        snapshots.append({**circuit, "timeline": timeline})
    Path(out_file).write_text(
        json.dumps({"circuits": snapshots, "waveforms": waveforms}, indent=1) + "\n"
    )


def _get_translation_kwargs(
    backend_config: BackendConfig, quantify_config: Any
) -> Dict[str, Any]:
    """Gets what QuantifyExecutor passed QuantifyExperiment.from_qobj_expt but the qobj"""
    device_config = backend_config.device_config
    return {
        "hardware_map": generate_hardware_map(
            qubit_ids=device_config.qubit_ids,
            coupling_dict=device_config.coupling_dict,
            quantify_config=quantify_config,
        ),
        "lo_frequencies": _extract_lo_frequencies(quantify_config),
        "drive_frequencies": _extract_drive_frequencies(backend_config),
    }


def _load_module(path: str) -> ModuleType:
    """Loads the module of the file, which needs nothing of this checkout"""
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    main()
