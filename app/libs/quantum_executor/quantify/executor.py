# This code is part of Tergite
#
# (C) Axel Andersson (2022)
# (C) Martin Ahindura (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Refactored by Martin Ahindura (2024)
# Refactored by Stefan Hill (2024)
# Refactored by Chalmers Next Labs 2025

"""
This module implements the executor.
"""

import copy
import os
from datetime import datetime
from typing import List, Optional, Union

import qblox_instruments
from qiskit.qobj import PulseQobj
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice
from quantify_scheduler.instrument_coordinator import InstrumentCoordinator
from quantify_scheduler.instrument_coordinator.components.qblox import ClusterComponent

from app.libs.device_parameters.dtos import BackendConfig
from app.libs.quantum_executor.base.executor import QuantumExecutor
from app.libs.quantum_executor.base.quantum_job import get_experiment_name
from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from app.libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from app.libs.quantum_executor.quantify.experiment import QuantifyExperiment
from app.libs.quantum_executor.utils.config import (
    QuantifyMetadata,
    load_quantify_config,
)
from app.libs.quantum_executor.utils.logger import ExperimentLogger
from app.libs.quantum_executor.utils.portclock import generate_hardware_map
from .spi_dac import SpiDAC
from settings import REDIS_CONNECTION


class QuantifyExecutor(QuantumExecutor):
    """The controller of the hardware that executes the quantum jobs"""

    _coordinator: Optional[InstrumentCoordinator] = None

    def __init__(
        self,
        quantify_config_file: Union[str, bytes, os.PathLike],
        quantify_metadata_file: Union[str, bytes, os.PathLike],
        backend_config: BackendConfig,
    ):
        self.quantify_config = load_quantify_config(quantify_config_file)

        qubit_ids = backend_config.device_config.qubit_ids
        coupling_dict = backend_config.device_config.coupling_dict

        # --- Initialize executor and hardware ---
        self.hardware_map = generate_hardware_map(
            qubit_ids=qubit_ids,
            coupling_dict=coupling_dict,
            quantify_config=self.quantify_config,
        )
        super().__init__(hardware_map=self.hardware_map)

        # make sure all previous connections are closed
        qblox_instruments.Cluster.close_all()

        # Initialize a (singleton) instrument coordinator if not already set.
        if QuantifyExecutor._coordinator is None:
            QuantifyExecutor._coordinator = InstrumentCoordinator(
                "tergite_quantum_executor"
            )

        clusters = QuantifyMetadata.from_yaml(quantify_metadata_file).get_clusters()
        for cluster in clusters:
            cluster.reset()  # resets cluster for consistency
            self._coordinator.add_component(ClusterComponent(cluster))

    def _to_native_experiments(
        self, qobj: PulseQobj, native_config: NativeQobjConfig, /
    ) -> List[QuantifyExperiment]:
        native_experiments = [
            QuantifyExperiment.from_qobj_expt(
                name=get_experiment_name(expt.header.name, idx + 1),
                expt=expt,
                qobj_config=qobj.config,
                hardware_map=self.hardware_map,
                native_config=native_config,
            )
            for idx, expt in enumerate(qobj.experiments)
        ]
        return native_experiments

    def _baseline_couplers(self, spi_dac: SpiDAC) -> None:
        zero_currents = {cplr: 0e-6 for cplr in self._couplers}
        spi_dac.set_dac_current(zero_currents)

        # Persist the parking current (0 A) in Redis for each coupler
        # TODO: couplers ref names would be different than expected here
        # debug and parse properly
        for cplr in self._couplers:
            REDIS_CONNECTION.hset(f"couplers:{cplr}", "parking_current", 0e-6)

    def _run_native(
        self,
        experiment: QuantifyExperiment,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        # Stop any running sequences.
        self._coordinator.stop()
        t1 = datetime.now()
        schedule_to_compile = copy.deepcopy(experiment.schedule)

        quantum_device = QuantumDevice("DUT")
        clean_config = self.quantify_config
        quantum_device.hardware_config(clean_config)

        compiler = SerialCompiler(name="compiler")
        compiled_schedule = compiler.compile(
            schedule=schedule_to_compile,
            config=quantum_device.generate_compilation_config(),
        )
        t2 = datetime.now()
        print(t2 - t1, "DURATION OF COMPILING")

        logger.log_Q1ASM_programs(compiled_schedule)
        logger.log_schedule(compiled_schedule)

        spi_dac = SpiDAC(couplers=self._couplers)
        self._baseline_couplers(spi_dac)

        bias_currents = self._extract_bias(experiment)
        if bias_currents:
            spi_dac.set_dac_current(bias_currents)

        self._coordinator.prepare(compiled_schedule)
        t3 = datetime.now()
        self._coordinator.start()
        self._coordinator.wait_done(timeout_sec=10)
        results = self._coordinator.retrieve_acquisition()
        print(f"{results=}")
        t4 = datetime.now()
        print(t4 - t3, "DURATION OF MEASURING")

        spi_dac.set_parking_currents(self._couplers)
        spi_dac.close_spi_rack()

        return QExperimentResult.from_xarray(results)

    def _extract_bias(self, expt: QuantifyExperiment) -> dict[str, float]:
        """
        Return {port_name: current[A]} for every WACQT-CZ instruction.
        """
        bias: dict[str, float] = {}
        for ch in expt.channel_registry.values():
            for inst in ch.instructions:
                if inst.name == "wacqt_cz" and "dc_bias" in inst.parameters:
                    # TODO: port name would be different than expected
                    # debug and parse properly
                    port = inst.port
                    cur = float(inst.parameters["dc_bias"])
                    if port not in bias or abs(cur) > abs(bias[port]):
                        bias[port] = cur
        return bias

    @classmethod
    def close(cls):
        if cls._coordinator is not None:
            cls._coordinator.close_all()
            cls._coordinator = None
