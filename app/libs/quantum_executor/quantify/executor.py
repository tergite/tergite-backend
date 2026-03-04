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

import logging
import os
from datetime import datetime
from typing import Dict, List, Union

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

from .spi_dac import init_spi_dacs

worker_logger = logging.getLogger(__name__)


class QuantifyExecutor(QuantumExecutor):
    """The controller of the hardware that executes the quantum jobs"""

    # a cache that won't be cleared by the garbage collector
    _non_gc_instruments: Dict[str, Dict[str, Instrument]] = {}

    def __init__(
        self,
        quantify_config_file: Union[str, bytes, os.PathLike],
        quantify_metadata_file: Union[str, bytes, os.PathLike],
        backend_config: BackendConfig,
        *,
        should_restore_currents: bool = False,
        reset: bool = False,
    ):
        """
        Args:
            quantify_config_file: path to the quantify specific config file
            quantify_metadata_file: path to our custom quantify specific metadata
            backend_config: the general backend configuration regardless of executor type
            should_restore_currents: whether to restore current state; default = False
            reset: whether to reset the whole executor; default = False
        """
        self.quantify_config = load_quantify_config(quantify_config_file)
        self.quantify_metadata = QuantifyMetadata.from_yaml(quantify_metadata_file)
        self.device_name = backend_config.general_config.name
        self.should_restore_currents = should_restore_currents

        qubit_ids = backend_config.device_config.qubit_ids
        coupling_dict = backend_config.device_config.coupling_dict

        # --- Initialize executor and hardware ---
        self.hardware_map = generate_hardware_map(
            qubit_ids=qubit_ids,
            coupling_dict=coupling_dict,
            quantify_config=self.quantify_config,
        )
        super().__init__(hardware_map=self.hardware_map, backend_config=backend_config)

        self._couplers = sorted(backend_config.device_config.coupling_dict.keys())

        # Build maps between coupler IDs and Quantify/port names from the hardware_map.
        # Assumes generate_hardware_map has entries for 'uN' like: hardware_map['u1'] -> (clock, port).
        self._coupler_to_port = {
            u: self.hardware_map[u][1] for u in self._couplers if u in self.hardware_map
        }
        self._port_to_coupler = {port: u for u, port in self._coupler_to_port.items()}
        self.coordinator_name = f"{self.device_name}-executor"
        no_gc_instruments_cache = self.__class__._non_gc_instruments.setdefault(
            self.device_name, {}
        )
        try:
            self._coordinator = no_gc_instruments_cache[self.coordinator_name]
        except KeyError:
            # make sure all previous connections are closed
            # FIXME: This global is unnatural but QCoDeS' delegation force us to make
            #   all instruments globals
            qblox_instruments.Cluster.close_all()
            self._coordinator = InstrumentCoordinator(self.coordinator_name)

            # FIXME: Saving to the class variable just to escape the garbage collector
            #   since QCoDeS already keeps these instruments as globals
            #   and they raise errors if they already exist in QCoDeS
            no_gc_instruments_cache[self.coordinator_name] = self._coordinator

            clusters = self.quantify_metadata.get_clusters()
            for cluster in clusters:
                if reset:
                    cluster.reset()  # resets cluster for consistency

                cluster_component = ClusterComponent(cluster)
                component_name = self._coordinator.add_component(cluster_component)
                no_gc_instruments_cache[component_name] = cluster_component

        self.spi_dacs = init_spi_dacs(metadata=self.quantify_metadata)

        try:
            self._quantum_device = Instrument.find_instrument(
                self.device_name, QuantumDevice
            )
        except KeyError:
            self._quantum_device = QuantumDevice(self.device_name)

        self._quantum_device.hardware_config(self.quantify_config)
        self._compiler = SerialCompiler(name=f"{self.device_name}-compiler")
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

        initial_bias_currents_map = {}
        if self.should_restore_currents:
            initial_bias_currents_map = {
                spi_name: spi_dac.get_current_biases()
                for spi_name, spi_dac in self.spi_dacs.items()
            }

        bias_currents = self._extract_bias(experiment)
        if bias_currents:
            print("Bias currents requested: %s", bias_currents)
            for spi_dac in self.spi_dacs.values():
                spi_dac.ramp_to_target_currents(bias_currents)
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

        # reset SPI DACs
        for spi_name, spi_dac in self.spi_dacs.items():
            if self.should_restore_currents:
                # return currents to their original values
                initial_biases = initial_bias_currents_map[spi_name]
                spi_dac.ramp_to_target_currents(initial_biases)

            spi_dac.close()

        return QExperimentResult.from_xarray(results)

    def _extract_bias(self, expt: QuantifyExperiment) -> dict[str, float]:
        """Return {'uN': current[A]} for every WACQT-CZ instruction.
        If multiple pulses hit the same coupler, keep the largest |current|.

        Args:
            expt: The experiment to extract bias from.

        Returns:
            dictionary of coupler and maximum bias current to set to as got from experiment.
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
                        coupler = self._port_to_coupler[port]  # normalize to 'uN'
                    except KeyError as e:
                        raise KeyError(
                            f"Unknown coupler port '{port}'. "
                            f"Make sure hardware_map has an entry for the coupler "
                            f"and that it’s connected to canonical ID via _port_to_coupler."
                        ) from e

                    bias_current = float(inst.parameters[dc_bias_alias])
                    if coupler not in bias or abs(bias_current) > abs(bias[coupler]):
                        bias[coupler] = bias_current
        return bias

    def close(self) -> None:
        self._coordinator.stop()
        for spi_dac in self.spi_dacs.values():
            spi_dac.close()
        # FIXME: This global is unnatural but QCoDeS is forcing us to do this
        #   Unfortunately, this means closing one instance of this class closes
        #   all clusters of all other instances. But if we don't, __init__ will be a problem
        #   especially in automated tests
        qblox_instruments.Cluster.close_all()
        self._coordinator.close_all()
        self.__class__._non_gc_instruments[self.device_name].clear()
