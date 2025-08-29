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
import re
from datetime import datetime
from typing import List, Optional, Union

import qblox_instruments
from qcodes import Instrument
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
from settings import REDIS_CONNECTION, QUANTIFY_METADATA_FILE


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

        self._couplers = sorted(backend_config.device_config.coupling_dict.keys())

        # Build maps between coupler IDs and Quantify/port names from the hardware_map.
        # Assumes generate_hardware_map has entries for 'uN' like: hardware_map['u1'] -> (clock, port).
        self._coupler_to_port = {
            u: self.hardware_map[u][1] for u in self._couplers if u in self.hardware_map
        }
        self._port_to_coupler = {port: u for u, port in self._coupler_to_port.items()}

        # make sure all previous connections are closed
        qblox_instruments.Cluster.close_all()

        # Initialize a (singleton) instrument coordinator if not already set.
        if self.__class__._coordinator is None:
            self.__class__._coordinator = InstrumentCoordinator(
                "tergite_quantum_executor"
            )
            clusters = QuantifyMetadata.from_yaml(quantify_metadata_file).get_clusters()
            for cluster in clusters:
                cluster.reset()  # resets cluster for consistency
                self.__class__._coordinator.add_component(ClusterComponent(cluster))

        device_name = "DUT"
        try:
            self._quantum_device = Instrument.find_instrument(
                device_name, QuantumDevice
            )
        except KeyError:
            self._quantum_device = QuantumDevice(device_name)

        self._quantum_device.hardware_config(self.quantify_config)
        self._compiler = SerialCompiler(name="compiler")
        self._compilation_config = self._quantum_device.generate_compilation_config()

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

    def _baseline_couplers(self) -> None:
        # Persist the parking current (0 A) in Redis for each coupler
        for cplr in self._couplers:
            self.spi_dac._connection.hset(f"couplers:{cplr}", "parking_current", 1e-4)

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

        compiled_schedule = self._compiler.compile(
            schedule=experiment.schedule,
            config=self._compilation_config,
        )
        t2 = datetime.now()
        print(t2 - t1, "DURATION OF COMPILING")

        logger.log_Q1ASM_programs(compiled_schedule)
        logger.log_schedule(compiled_schedule)

        self.spi_dac = SpiDAC(
            couplers=self._couplers, metadata_path=QUANTIFY_METADATA_FILE
        )
        self._baseline_couplers()
        self.spi_dac.set_parking_currents(self._couplers)

        bias_currents = self._extract_bias(experiment)
        if bias_currents:
            print("Bias currents requested: %s", bias_currents)
            self.spi_dac.set_dac_current(bias_currents)
        else:
            print("No dc_bias extracted from schedule; skipping bias set.")

        self._coordinator.prepare(compiled_schedule)
        t3 = datetime.now()
        self._coordinator.start()
        self._coordinator.wait_done(timeout_sec=10)
        results = self._coordinator.retrieve_acquisition()
        print(f"{results=}")
        t4 = datetime.now()
        print(t4 - t3, "DURATION OF MEASURING")

        self.spi_dac.set_parking_currents(self._couplers)
        self.spi_dac.close_spi_rack()

        return QExperimentResult.from_xarray(results)

    def _extract_bias(self, expt: QuantifyExperiment) -> dict[str, float]:
        """
        Return {'uN': current[A]} for every WACQT-CZ instruction.
        If multiple pulses hit the same coupler, keep the largest |current|.
        """
        # TODO: very ad-hoc extraction, refactor later integrating better with microwave parameters extraction in experiment.py
        print("Scanning %d channels for dc_bias...", len(expt.channel_registry))
        bias: dict[str, float] = {}
        dc_bias_alias = "theta"
        for ch in expt.channel_registry.values():
            for inst in ch.instructions:
                if inst.name == "wacqt_cz" and dc_bias_alias in inst.parameters:
                    port = inst.port
                    try:
                        u = self._port_to_coupler[port]  # normalize to 'uN'
                    except KeyError as e:
                        raise KeyError(
                            f"Unknown coupler port '{port}'. "
                            f"Make sure hardware_map has an entry for the coupler "
                            f"and that it’s connected to canonical ID via _port_to_coupler."
                        ) from e

                    cur = float(inst.parameters[dc_bias_alias])
                    if u not in bias or abs(cur) > abs(bias[u]):
                        bias[u] = cur
        return bias

    @classmethod
    def close(cls):
        if cls._coordinator is not None:
            cls._coordinator.close_all()
            cls._coordinator = None
