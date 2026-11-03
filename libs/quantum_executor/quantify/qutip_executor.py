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
"""The executor of a simulated device, whose backend config has a simulator section: its
jobs compiled as on clusters and played on QuTiP"""
import uuid
from pathlib import Path
from typing import Any, List, Optional

import numpy as np
from quantify_core.data.handling import set_datadir
from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

from libs.device_parameters import BackendConfig, DeviceCalibration

from ..base.circuits import CircuitExperiment, QasmCircuit
from ..base.executor import QuantumExecutor
from ..base.quantum_job.dtos import NativeQobjConfig
from ..base.quantum_job.typing import QExperimentResult
from ..utils.logger import ExperimentLogger
from .defcals import add_pulse_calibration
from .device import apply_calibration
from .experiments import get_circuit_experiments
from .simulator import get_discriminators, get_simulated_device, run_schedule


class QutipExecutor(QuantumExecutor):
    """Runs a simulated device's jobs on QuTiP, each circuit compiled as the quantify
    executor compiles it, for a quantum device without hardware"""

    def __init__(
        self,
        backend_config: BackendConfig,
        *,
        data_dir: Path,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        """
        Args:
            backend_config: the backend config, with its calibration and simulator section
            data_dir: the directory where to save experiment data
            seed: the seed of the readout's outcomes and noise; none for a fresh one
            kwargs: the other options of an executor, which a simulated device ignores
        """
        set_datadir(data_dir)
        super().__init__(data_dir=data_dir, backend_config=backend_config)
        name = backend_config.general_config.name
        # its own name, as QCoDeS names instruments for the whole process
        self._quantum_device = QuantumDevice(f"{name}_simulated_{uuid.uuid4().hex}")
        apply_calibration(self._quantum_device, backend_config)
        self._compiler = SerialCompiler(name=f"{name}_compiler")
        self._simulated = get_simulated_device(self._quantum_device, backend_config)
        self._rng = np.random.default_rng(seed)
        # discriminating at the lines between each qubit's simulated IQ points
        backend_config.calibration_config.discriminators = {
            "lda": get_discriminators(self._simulated)
        }

    def to_circuit_experiments(
        self, circuits: List[QasmCircuit], native_config: NativeQobjConfig, /
    ) -> List[CircuitExperiment[Schedule]]:
        """Compiles the circuits of a gate-level job to gate-level schedules for the
        device's transmons, each circuit qubit i being the i-th qubit by its number

        Args:
            circuits: the circuits of the job
            native_config: the native config for the job

        Returns:
            an experiment per circuit, whose duration its compilation gives

        Raises:
            UnsupportedCircuitError: a circuit has a gate or qubits the device lacks
        """
        return get_circuit_experiments(
            self._quantum_device,
            self.backend_config,
            self._compiler,
            circuits,
            native_config,
        )

    def run_native(
        self,
        experiment: CircuitExperiment[Schedule],
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        """Compiles the experiment of a circuit for the device and plays it on QuTiP

        Args:
            experiment: the experiment to run
            native_config: native config for the job
            logger: the logger for the given experiment, which also logs its schedule

        Returns:
            xarray.Dataset of results, one data variable per acquisition channel
        """
        compiled = self._compiler.compile(
            schedule=experiment.schedule,
            config=self._quantum_device.generate_compilation_config(),
        )
        logger.log_schedule(compiled)
        return run_schedule(compiled, self._simulated, native_config.shots, self._rng)

    def recalibrate(self, **kwargs: Any) -> None:
        """Does nothing, as a simulated device keeps its calibration"""
        return None

    def add_pulse_calibration(
        self, calibration: DeviceCalibration
    ) -> DeviceCalibration:
        """Adds to the calibration the frames the device's pulse-level jobs may play on and
        its gates' defcals; none if its backend config sets no pulse limits

        Args:
            calibration: the device's calibration, of this executor's backend config

        Returns:
            the calibration, with the frames and defcals where the device takes
            pulse-level jobs
        """
        return add_pulse_calibration(calibration, self.backend_config)

    def close(self) -> None:
        """Closes the quantum device"""
        self._quantum_device.close()
