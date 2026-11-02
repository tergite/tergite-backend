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

from libs.device_parameters.dtos import BackendConfig
from libs.qiskit.qobj import PulseQobj
from libs.quantum_executor.base.circuits import CircuitExperiment, QasmCircuit
from libs.quantum_executor.base.executor import QuantumExecutor
from libs.quantum_executor.base.qasm import to_native_circuit
from libs.quantum_executor.base.quantum_job import get_experiment_name
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from libs.quantum_executor.quantify.circuits import (
    get_circuit_schedule,
    get_measure_duration,
)
from libs.quantum_executor.quantify.device import (
    apply_calibration,
    get_edge_names,
    get_element_name,
    remove_elements,
)
from libs.quantum_executor.quantify.experiment import QuantifyExperiment
from libs.quantum_executor.quantify.utils.config import (
    InstrumentConfig,
    QuantifyMetadata,
    load_quantify_config,
)
from libs.quantum_executor.quantify.utils.portclock import get_hardware_map
from libs.quantum_executor.utils.logger import ExperimentLogger
from utils.compat import MeasurementMode, SPIMode

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
        """
        self.calib_seed_file = calib_seed_file
        self.calib_spi_conf = calib_spi_conf
        self.calib_device_conf = calib_device_conf
        self.calib_node_conf = calib_node_conf
        self.quantify_config = load_quantify_config(quantify_config_file)
        self.quantify_metadata = QuantifyMetadata.from_yaml(quantify_metadata_file)
        self.device_name = backend_config.general_config.name
        self.is_cluster_reset_allowed = is_cluster_reset_allowed
        self.lo_frequencies = _extract_lo_frequencies(self.quantify_config)
        self.drive_frequencies = _extract_drive_frequencies(backend_config)

        qubit_ids = backend_config.device_config.qubit_ids
        coupling_dict = backend_config.device_config.coupling_dict

        self.hardware_map = get_hardware_map(
            qubit_ids=qubit_ids, coupling_dict=coupling_dict
        )

        set_datadir(data_dir)
        super().__init__(
            data_dir=data_dir,
            hardware_map=self.hardware_map,
            backend_config=backend_config,
        )
        self._experiment_delay = self.quantify_metadata.experiment_delay

        self.coordinator_name = f"{self.device_name}_executor"
        # keyed by the metadata the clusters come from, so an executor of other hardware with
        # the same device name never reuses them
        self._instruments_key = str(Path(os.fsdecode(quantify_metadata_file)).resolve())
        self._coordinator = self._get_coordinator(
            should_reset_clusters=should_reset and is_cluster_reset_allowed
        )
        self._quantum_device = _get_quantum_device(
            self.device_name, self.quantify_config
        )
        self._compiler = SerialCompiler(name=f"{self.device_name}_compiler")
        self._compilation_config = self._quantum_device.generate_compilation_config()
        # gate-level jobs compile against transmons, which pulse-level ones must not see
        self._gate_level_device = _get_quantum_device(
            f"{self.device_name}_gate_level", self.quantify_config
        )
        apply_calibration(self._gate_level_device, backend_config)
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
        remove_elements(self._gate_level_device)
        results = self._recalibrate_cluster(*cluster)
        if isinstance(results, DeviceCalibration):
            logging.info(f"calibration complete. Saving to {self.calib_seed_file}...")
            _save_calibration_seed(results, self.calib_seed_file)
            # so that the jobs after it run on it, not on the calibration it began with
            self.backend_config = self.backend_config.model_copy(
                update={"calibration_config": results.to_calibration_config()}
            )
            self.drive_frequencies = _extract_drive_frequencies(self.backend_config)
        return results

    def _connect_if_stale(self) -> None:
        """Connects the clusters and gets both quantum devices anew, if a recalibration
        left them closed"""
        if not self._is_stale:
            return
        self._coordinator = self._create_coordinator(should_reset_clusters=False)
        self._load_quantum_devices()
        self._is_stale = False

    def _load_quantum_devices(self) -> None:
        """Gets both quantum devices anew, with the calibration's transmons on the
        gate-level one: tergite_tuner's Instrument.close_all may have closed them"""
        self._quantum_device = _get_quantum_device(
            self.device_name, self.quantify_config
        )
        self._compilation_config = self._quantum_device.generate_compilation_config()
        self._gate_level_device = _get_quantum_device(
            f"{self.device_name}_gate_level", self.quantify_config
        )
        apply_calibration(self._gate_level_device, self.backend_config)

    def to_native_experiments(
        self, qobj: PulseQobj, native_config: NativeQobjConfig, /
    ) -> List[QuantifyExperiment]:
        """Constructs quantify experiments from the PulseQobj instance

        Args:
            qobj: the Pulse qobject containing the experiments
            native_config: the native config for the qobj

        Returns:
            list of QuantifyExperiment's
        """
        native_experiments = [
            QuantifyExperiment.from_qobj_expt(
                name=get_experiment_name(expt.header.name, idx + 1),
                expt=expt,
                qobj_config=qobj.config,
                hardware_map=self.hardware_map,
                native_config=native_config,
                lo_frequencies=self.lo_frequencies,
                drive_frequencies=self.drive_frequencies,
            )
            for idx, expt in enumerate(qobj.experiments)
        ]
        return native_experiments

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
        qubit_ids = self.backend_config.device_config.qubit_numbers
        element_names = [get_element_name(qubit_id) for qubit_id in qubit_ids]
        edge_names = get_edge_names(self._gate_level_device)
        measure_durations = self._get_measure_durations(element_names)
        compilation_config = self._gate_level_device.generate_compilation_config()

        experiments = []
        for idx, circuit in enumerate(circuits):
            native_circuit = to_native_circuit(
                circuit, dt=self.backend_config.general_config.dt
            )
            schedule = get_circuit_schedule(
                native_circuit,
                native_config,
                element_names=element_names,
                edge_names=edge_names,
                measure_durations=measure_durations,
            )
            compiled = self._compiler.compile(schedule, config=compilation_config)
            experiments.append(
                CircuitExperiment(
                    name=get_experiment_name(circuit.name, idx + 1),
                    schedule=schedule,
                    duration=compiled.duration,
                    readout=native_circuit.to_readout(qubit_ids),
                )
            )
        return experiments

    def run_native(
        self,
        experiment: Union[QuantifyExperiment, CircuitExperiment[Schedule]],
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        """Compiles the experiment and runs it on the clusters

        Args:
            experiment: the native experiment to run, of a qobj or of a circuit
            native_config: native config for the qobj
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

        compilation_config = self._compilation_config
        if isinstance(experiment, CircuitExperiment):
            compilation_config = self._gate_level_device.generate_compilation_config()
        compiled_schedule = self._compiler.compile(
            schedule=experiment.schedule,
            config=compilation_config,
        )

        logger.log_Q1ASM_programs(compiled_schedule)
        logger.log_schedule(compiled_schedule)

        self._coordinator.prepare(compiled_schedule)
        self._coordinator.start()
        self._coordinator.wait_done(timeout_sec=10)
        results = self._coordinator.retrieve_acquisition()

        return results

    def _get_measure_durations(self, element_names: List[str]) -> Dict[str, float]:
        """Gets the seconds each of the transmons' measurement takes, by its name, leaving
        out those the device lacks, as their qubits are not calibrated"""
        durations = {}
        for name in element_names:
            if name not in self._gate_level_device.elements():
                continue
            measure = self._gate_level_device.get_element(name).measure
            durations[name] = get_measure_duration(
                pulse_duration=measure.pulse_duration(),
                acq_delay=measure.acq_delay(),
                integration_time=measure.integration_time(),
            )
        return durations

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
                cluster_component = ClusterComponent(cluster)
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
        """Recalibrates the device through the cluster with tergite-tuner"""
        backend_config = self.backend_config
        cluster_mode = MeasurementMode.dummy if conf.is_dummy else MeasurementMode.real
        logging.info(f"recalibrating {name} in {cluster_mode} mode...")
        return recalibrate(
            backend_config=backend_config,
            results_file=Path(self.calib_seed_file).with_suffix(".tuner.json"),
            cluster_mode=cluster_mode,
            stdout_log_level=25,
            file_log_level=25,
            cluster_ip=conf.ip_address,
            data_dir=Path(self.data_dir),
            spi_mode=SPIMode.dummy,
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


def _extract_lo_frequencies(quantify_config: Any) -> Dict[str, float]:
    modulation_frequencies = (
        getattr(quantify_config.hardware_options, "modulation_frequencies", {}) or {}
    )
    results: Dict[str, float] = {}
    for key, entry in modulation_frequencies.items():
        lo_freq = getattr(entry, "lo_freq", None)
        if lo_freq is None and isinstance(entry, dict):
            lo_freq = entry.get("lo_freq")
        if lo_freq is None:
            continue
        results[key] = float(lo_freq)

    return results


def _extract_drive_frequencies(backend_config: BackendConfig) -> Dict[str, float]:
    calibration_config = backend_config.calibration_config
    if calibration_config is None:
        return {}

    results: Dict[str, float] = {}
    for qubit in calibration_config.qubit:
        qubit_id = qubit.get("id")
        frequency_hz = qubit.get("frequency")
        if qubit_id is None or frequency_hz is None:
            continue

        results[_to_drive_clock(qubit_id)] = float(frequency_hz)

    return results


def _to_drive_clock(qubit_id: Any) -> str:
    stripped = str(qubit_id).strip().lstrip("q")
    return f"q{int(float(stripped)):02d}.01"


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
        dummy_cfg = {
            int(idx): _QBLOX_CLUSTER_TYPE_MAP[module.instrument_type]
            for idx, module in conf.modules.items()
        }

    return qblox_instruments.Cluster(
        name=name,
        identifier=conf.ip_address,
        dummy_cfg=dummy_cfg,
    )


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
