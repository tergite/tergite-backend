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
This module implements the quantify executor.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import qblox_instruments
import toml
from qcodes import Instrument
from quantify_core.data.handling import set_datadir
from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice
from quantify_scheduler.instrument_coordinator import InstrumentCoordinator
from quantify_scheduler.instrument_coordinator.components.qblox import ClusterComponent
from tergite_tuner.utils.types.enums import MeasurementMode, SPIMode
from threadpoolctl import threadpool_limits

from libs.device_parameters.dtos import BackendConfig
from libs.quantum_executor.base.circuits import CircuitExperiment, QasmCircuit
from libs.quantum_executor.base.executor import QuantumExecutor
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from libs.quantum_executor.quantify.defcals import add_pulse_calibration
from libs.quantum_executor.quantify.device import apply_calibration, remove_elements
from libs.quantum_executor.quantify.experiments import get_circuit_experiments
from libs.quantum_executor.quantify.simulated_chip.component import (
    SimulatingClusterComponent,
)
from libs.quantum_executor.quantify.simulated_chip.engine import SimulatedChip
from libs.quantum_executor.quantify.simulated_chip.tuner import connect_tuner_to_chip
from libs.quantum_executor.quantify.utils.config import (
    InstrumentConfig,
    QuantifyMetadata,
    load_quantify_config,
)
from libs.quantum_executor.utils.logger import ExperimentLogger

from ...device_parameters import DeviceCalibration
from .utils.calibration import recalibrate

_QBLOX_CLUSTER_TYPE_MAP: Dict[str, qblox_instruments.ClusterType] = {
    "QCM": qblox_instruments.ClusterType.CLUSTER_QCM,
    "QRM": qblox_instruments.ClusterType.CLUSTER_QRM,
    "QCM_RF": qblox_instruments.ClusterType.CLUSTER_QCM_RF,
    "QRM_RF": qblox_instruments.ClusterType.CLUSTER_QRM_RF,
}


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
        should_reset: bool = False,
        is_cluster_reset_allowed: bool = False,
        data_dir: Path | os.PathLike[str],
        calib_node_conf: Path | os.PathLike[str],
        calib_device_conf: Path | os.PathLike[str],
        calib_spi_conf: Path | os.PathLike[str],
        calib_seed_file: Path | os.PathLike[str],
        **kwargs,
    ):
        """
        Args:
            quantify_config_file: path to the quantify specific config file
            quantify_metadata_file: path to our custom quantify specific metadata
            backend_config: the general backend configuration regardless of executor type
            should_reset: whether to reset the whole executor; default = False
            is_cluster_reset_allowed: whether the clusters can be reset for this executor; default = False
            data_dir: the directory where to save experiment data
            calib_node_conf: the configuration file for the nodes during calibration
            calib_device_conf: the configuration file for the entire devices during calibration
            calib_spi_conf: the configuration file for the spi during calibration
            calib_seed_file: the calibration seed file

        Raises:
            FileNotFoundError: tergite-tuner's config {file} not found
        """
        _check_tuner_configs_exist(calib_node_conf, calib_device_conf, calib_spi_conf)
        self.calib_seed_file = calib_seed_file
        self.calib_spi_conf = calib_spi_conf
        self.calib_device_conf = calib_device_conf
        self.calib_node_conf = calib_node_conf
        self.quantify_config = load_quantify_config(quantify_config_file)
        self.quantify_metadata = QuantifyMetadata.from_yaml(quantify_metadata_file)
        self.device_name = backend_config.general_config.name
        self.is_cluster_reset_allowed = is_cluster_reset_allowed

        set_datadir(data_dir)
        super().__init__(data_dir=data_dir, backend_config=backend_config)
        self._experiment_delay = self.quantify_metadata.experiment_delay
        self._chip = _get_simulated_chip(backend_config, self.quantify_metadata)

        self.coordinator_name = f"{self.device_name}_executor"
        # keyed by the metadata the clusters come from, and whether a simulated chip is
        # behind them, so an executor of other hardware with the same device name never
        # reuses them
        self._instruments_key = str(Path(os.fsdecode(quantify_metadata_file)).resolve())
        if self._chip is not None:
            self._instruments_key += ":simulated-chip"
        self._coordinator = self._get_coordinator(
            should_reset_clusters=should_reset and is_cluster_reset_allowed
        )
        self._compiler = SerialCompiler(name=f"{self.device_name}_compiler")
        self._quantum_device = _get_quantum_device(
            self.device_name, self.quantify_config
        )
        apply_calibration(self._quantum_device, backend_config)
        # whether a recalibration left the clusters and quantum devices to get anew
        self._is_stale = False

    def recalibrate(self, **kwargs) -> DeviceCalibration | None:
        """Recalibrates the executor

        Returns:
            the final device calibration state after recalibration
        """
        cluster = _get_recalibration_cluster(self.quantify_metadata)
        if cluster is None:
            return None

        # tergite_tuner creates its own transmons by these names, and closes every cluster
        # as it connects its own, this executor's too; the next job gets them anew, not this
        # recalibration, whose time limit could interrupt that halfway
        self._is_stale = True
        remove_elements(self._quantum_device)
        results = self._recalibrate_cluster(*cluster)
        if isinstance(results, DeviceCalibration):
            logging.info(f"calibration complete. Saving to {self.calib_seed_file}...")
            _save_calibration_seed(results, self.calib_seed_file)
            # so that the jobs after it run on it, not on the calibration it began with
            self.backend_config = self.backend_config.model_copy(
                update={"calibration_config": results.to_calibration_config()}
            )
        return results

    def add_pulse_calibration(
        self, calibration: DeviceCalibration
    ) -> DeviceCalibration:
        """Adds to the calibration the frames the device's pulse-level jobs may play on and
        its gates' defcals, as its gate-level jobs play them; none if its backend config
        sets no pulse limits, as it then takes no pulse-level jobs

        Args:
            calibration: the device's calibration, of this executor's backend config

        Returns:
            the calibration, with the frames and defcals where the device takes
            pulse-level jobs
        """
        return add_pulse_calibration(calibration, self.backend_config)

    def _connect_if_stale(self) -> None:
        """Connects the clusters and gets the quantum device anew, if a recalibration
        left them closed"""
        if not self._is_stale:
            return
        self._coordinator = self._create_coordinator(should_reset_clusters=False)
        self._load_quantum_device()
        self._is_stale = False

    def _load_quantum_device(self) -> None:
        """Gets the quantum device anew, with the calibration's transmons:
        tergite_tuner's Instrument.close_all may have closed them"""
        self._quantum_device = _get_quantum_device(
            self.device_name, self.quantify_config
        )
        apply_calibration(self._quantum_device, self.backend_config)

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
        self._connect_if_stale()
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
        """Compiles the experiment of a circuit and runs it on the clusters

        Args:
            experiment: the experiment to run
            native_config: native config for the job
            logger: the logger for the given experiment, which also logs its schedule
                and Q1ASM programs

        Returns:
            xarray.Dataset of results, one data variable per acquisition channel
        """
        if self._experiment_delay > 0:
            time.sleep(self._experiment_delay)

        self._connect_if_stale()
        # Stop any running sequences.
        self._coordinator.stop()

        compiled_schedule = self._compiler.compile(
            schedule=experiment.schedule,
            config=self._quantum_device.generate_compilation_config(),
        )

        logger.log_Q1ASM_programs(compiled_schedule)
        logger.log_schedule(compiled_schedule)

        self._coordinator.prepare(compiled_schedule)
        self._coordinator.start()
        self._coordinator.wait_done(timeout_sec=10)
        results = self._coordinator.retrieve_acquisition()

        return results

    def _get_coordinator(self, should_reset_clusters: bool) -> InstrumentCoordinator:
        """Gets the coordinator of the executor's clusters, cached across executors of the
        same metadata, or else creates it"""
        cached_instruments = self.__class__._non_gc_instruments.get(
            self._instruments_key, {}
        )
        try:
            return cached_instruments[self.coordinator_name]
        except KeyError:
            return self._create_coordinator(should_reset_clusters)

    def _create_coordinator(self, should_reset_clusters: bool) -> InstrumentCoordinator:
        """Creates the coordinator of the clusters the metadata lists, and caches it, once
        the cached instruments, which none can then use, are closed"""
        # the cached components first, as closing one reads its cluster
        # FIXME: This global is unnatural but QCoDeS' delegation force us to make
        #   all instruments globals
        self.__class__._close_cached_instruments()
        qblox_instruments.Cluster.close_all()

        # FIXME: Saving to the class variable just to escape the garbage collector
        #   since QCoDeS already keeps these instruments as globals
        #   and they raise errors if they already exist in QCoDeS
        cached_instruments = self.__class__._non_gc_instruments.setdefault(
            self._instruments_key, {}
        )
        coordinator = InstrumentCoordinator(self.coordinator_name)
        try:
            for cluster in _create_clusters(self.quantify_metadata):
                if should_reset_clusters:
                    cluster.reset()
                cluster_component = (
                    ClusterComponent(cluster)
                    if self._chip is None
                    else SimulatingClusterComponent(cluster, self._chip)
                )
                component_name = coordinator.add_component(cluster_component)
                cached_instruments[component_name] = cluster_component
        except Exception:
            coordinator.close()
            raise
        # cached only once it has its clusters: cached before, a cluster that could not be
        # reached left the next executor a coordinator without them, and every job failed
        cached_instruments[self.coordinator_name] = coordinator
        return coordinator

    def _recalibrate_cluster(
        self, name: str, conf: InstrumentConfig
    ) -> DeviceCalibration | None:
        """Recalibrates the device through the cluster with tergite-tuner: a simulated
        chip as the tuner would real hardware, its cluster a dummy the chip is behind"""
        if self._chip is None:
            return self._run_tuner(name, conf, is_real=not conf.is_dummy)
        with connect_tuner_to_chip(_get_dummy_modules(conf), self._chip):
            return self._run_tuner(name, conf, is_real=True)

    def _run_tuner(
        self, name: str, conf: InstrumentConfig, is_real: bool
    ) -> DeviceCalibration | None:
        """Runs tergite-tuner's recalibration through the cluster, in its real cluster
        mode or its dummy one"""
        backend_config = self.backend_config
        cluster_mode = MeasurementMode.real if is_real else MeasurementMode.dummy
        logging.info(f"recalibrating {name} in {cluster_mode} mode...")
        return recalibrate(
            backend_config=backend_config,
            results_file=Path(self.calib_seed_file).with_suffix(".tuner.json"),
            cluster_mode=cluster_mode,
            stdout_log_level=25,
            file_log_level=25,
            cluster_ip=conf.ip_address,
            data_dir=Path(self.data_dir),
            **_get_spi_options(self.quantify_metadata),
            qubits=backend_config.device_config.qubit_ids,
            couplers=[
                f"{q1}_{q2}"
                for q1, q2 in backend_config.device_config.coupling_dict.values()
            ],
            is_recalibration=True,
            ignore_spec=True,
            save_plot=False,
            fixed_duration_couplers=backend_config.device_config.fixed_duration_couplers,
            reverse_phase_qubits=backend_config.device_config.reverse_phase_qubits,
            cluster_config=self.quantify_config,
            spi_config=self.calib_spi_conf,
            device_config=self.calib_device_conf,
            node_config=self.calib_node_conf,
        )

    def close(self) -> None:
        """Stops and closes the clusters, those of every other QuantifyExecutor too"""
        # a recalibration's tuner has closed them already
        if not self._is_stale:
            self._coordinator.stop()
        # FIXME: This global is unnatural but QCoDeS is forcing us to do this
        #   Unfortunately, this means closing one instance of this class closes
        #   all clusters of all other instances. But if we don't, __init__ will be a problem
        #   especially in automated tests
        qblox_instruments.Cluster.close_all()
        self._coordinator.close_all()
        self.__class__._non_gc_instruments.clear()

    @classmethod
    def _close_cached_instruments(cls) -> None:
        for instruments in cls._non_gc_instruments.values():
            for instrument in instruments.values():
                # Instrument.close_all, which tergite_tuner calls, may have closed it
                if Instrument.is_valid(instrument):
                    instrument.close()
        cls._non_gc_instruments.clear()


def _save_calibration_seed(calibration: DeviceCalibration, file: Path) -> None:
    """Writes the calibration to the seed file, which the backend config is read with"""
    seed = {
        "calibration_config": calibration.to_calibration_config().model_dump(
            mode="json"
        )
    }
    with open(file, "w") as seed_file:
        toml.dump(seed, seed_file)


def _create_clusters(metadata: QuantifyMetadata) -> List[qblox_instruments.Cluster]:
    """Creates the clusters the metadata lists, which connects to the real ones"""
    return [
        _create_cluster(name, conf)
        for name, conf in metadata.root.items()
        if conf.instrument_type == "Cluster"
    ]


def _create_cluster(name: str, conf: InstrumentConfig) -> qblox_instruments.Cluster:
    """
    Creates and initializes a Cluster object.

    If the configuration indicates a dummy cluster, a dummy configuration is built.
    Otherwise, a real cluster is instantiated and reset.

    Args:
        name: the name of the cluster
        conf: the configuration of the cluster

    Returns:
        qblox_instruments.Cluster for given name and config
    """
    dummy_cfg = None
    if getattr(conf, "is_dummy", False):
        dummy_cfg = _get_dummy_modules(conf)

    return qblox_instruments.Cluster(
        name=name,
        identifier=conf.ip_address,
        dummy_cfg=dummy_cfg,
    )


def _get_dummy_modules(
    conf: InstrumentConfig,
) -> Dict[int, qblox_instruments.ClusterType]:
    """Gets the modules of a dummy cluster of the config, by slot"""
    return {
        int(idx): _QBLOX_CLUSTER_TYPE_MAP[module.instrument_type]
        for idx, module in conf.modules.items()
    }


def _get_simulated_chip(
    backend_config: BackendConfig, metadata: QuantifyMetadata
) -> Optional[SimulatedChip]:
    """Gets the simulated chip of the backend config's simulator section, behind the
    metadata's dummy instruments, following the DC currents set on its SPI rack if it
    lists one; none for a device on clusters

    Raises:
        ValueError: a simulated chip plays behind dummy instruments, but {name} is not one
    """
    simulator = backend_config.simulator
    if simulator is None:
        return None
    for name, conf in metadata.root.items():
        if not conf.is_dummy:
            raise ValueError(
                f"a simulated chip plays behind dummy instruments, but {name} is not one"
            )
    # BLAS starts a thread for each core it sees, which, under a CPU quota or beside
    # another simulated device, starve one another on the chip's small matrices
    threadpool_limits(limits=1)
    return SimulatedChip(
        simulator.chip, should_follow_currents=_get_spi_rack(metadata) is not None
    )


def _get_spi_rack(metadata: QuantifyMetadata) -> Optional[InstrumentConfig]:
    """Gets the config of the SPI rack the metadata lists, or None if it lists none"""
    racks = [
        conf for conf in metadata.root.values() if conf.instrument_type == "SPI-Rack"
    ]
    return racks[0] if racks else None


def _get_spi_options(metadata: QuantifyMetadata) -> Dict[str, Any]:
    """Gets tergite-tuner's options of the metadata's SPI rack: its real one, on its
    port, unless the rack is a dummy or there is none"""
    rack = _get_spi_rack(metadata)
    if rack is None or rack.is_dummy:
        return {"spi_mode": SPIMode.dummy}
    return {"spi_mode": SPIMode.real, "spi_serial_port": rack.port}


def _get_quantum_device(name: str, quantify_config: Any) -> QuantumDevice:
    """Gets the quantum device of the name, which QCoDeS keeps once made, with the
    hardware config"""
    try:
        device = Instrument.find_instrument(name, QuantumDevice)
    except KeyError:
        device = QuantumDevice(name)
    device.hardware_config(quantify_config)
    return device


def _get_recalibration_cluster(
    metadata: QuantifyMetadata,
) -> Optional[Tuple[str, InstrumentConfig]]:
    """Gets the name and config of the one cluster the metadata lists, which tergite-tuner
    recalibrates through, or None if it lists none

    Raises:
        ValueError: recalibration runs through one cluster, ... but the quantify metadata lists {n}
    """
    clusters = [
        (name, conf)
        for name, conf in metadata.root.items()
        if conf.instrument_type == "Cluster"
    ]
    if len(clusters) > 1:
        raise ValueError(
            "recalibration runs through one cluster, as tergite_tuner does, "
            f"but the quantify metadata lists {len(clusters)}"
        )
    return clusters[0] if clusters else None


def _check_tuner_configs_exist(*files: Path | os.PathLike[str]) -> None:
    """Checks that the configs of tergite-tuner, which recalibrates the device, exist

    Raises:
        FileNotFoundError: tergite-tuner's config {file} not found
    """
    for file in files:
        if not Path(file).exists():
            raise FileNotFoundError(f"tergite-tuner's config {file} not found")
