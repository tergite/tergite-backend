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
from pydantic import RedisDsn
from qcodes import Instrument
from quantify_core.data.handling import set_datadir
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice
from quantify_scheduler.instrument_coordinator import InstrumentCoordinator
from quantify_scheduler.instrument_coordinator.components.qblox import ClusterComponent

from app.libs.device_parameters.dtos import BackendConfig
from app.libs.qiskit.qobj import PulseQobj
from app.libs.quantum_executor.base.executor import QuantumExecutor
from app.libs.quantum_executor.base.quantum_job import get_experiment_name
from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from app.libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from app.libs.quantum_executor.quantify.experiment import QuantifyExperiment
from app.libs.quantum_executor.quantify.utils.config import (
    InstrumentConfig,
    QuantifyMetadata,
    load_quantify_config,
)
from app.libs.quantum_executor.quantify.utils.portclock import generate_hardware_map
from app.libs.quantum_executor.utils.logger import ExperimentLogger
from app.utils.compat import MeasurementMode, SPIMode

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
        reset: bool = False,
        are_clusters_resettable: bool = False,
        data_dir: Path | os.PathLike[str],
        calib_node_conf: Path | os.PathLike[str],
        calib_device_conf: Path | os.PathLike[str],
        calib_spi_conf: Path | os.PathLike[str],
        calib_seed_file: Path | os.PathLike[str],
        redis_url: RedisDsn | str,
        **kwargs,
    ):
        """
        Args:
            quantify_config_file: path to the quantify specific config file
            quantify_metadata_file: path to our custom quantify specific metadata
            backend_config: the general backend configuration regardless of executor type
            reset: whether to reset the whole executor; default = False
            are_clusters_resettable: whether the clusters can be reset for this executor; default = False
            data_dir: the directory where to save experiment data
            calib_node_conf: the configuration file for the nodes during calibration
            calib_device_conf: the configuration file for the entire devices during calibration
            calib_spi_conf: the configuration file for the spi during calibration
            calib_seed_file: the calibration seed file
            redis_url: the redis url where data, e.g. recalibration data, is temporarily stored
        """
        self.calib_seed_file = calib_seed_file
        self.calib_spi_conf = calib_spi_conf
        self.calib_device_conf = calib_device_conf
        self.calib_node_conf = calib_node_conf
        self.quantify_config = load_quantify_config(quantify_config_file)
        self.quantify_metadata = QuantifyMetadata.from_yaml(quantify_metadata_file)
        self.device_name = backend_config.general_config.name
        self.are_clusters_resettable = are_clusters_resettable
        self.lo_frequencies = _extract_lo_frequencies(self.quantify_config)
        self.drive_frequencies = _extract_drive_frequencies(backend_config)
        self._redis_url = redis_url

        qubit_ids = backend_config.device_config.qubit_ids
        coupling_dict = backend_config.device_config.coupling_dict

        self.hardware_map = generate_hardware_map(
            qubit_ids=qubit_ids,
            coupling_dict=coupling_dict,
            quantify_config=self.quantify_config,
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
            should_reset_clusters=reset and are_clusters_resettable
        )
        self._quantum_device = _get_quantum_device(
            self.device_name, self.quantify_config
        )
        self._compiler = SerialCompiler(name=f"{self.device_name}_compiler")
        self._compilation_config = self._quantum_device.generate_compilation_config()

    def recalibrate(self, **kwargs) -> DeviceCalibration | None:
        """Recalibrates the executor

        Returns:
            the final device calibration state after recalibration
        """
        cluster = _get_recalibration_cluster(self.quantify_metadata)
        if cluster is None:
            return None

        results = self._recalibrate_cluster(*cluster)
        if isinstance(results, DeviceCalibration):
            logging.info(f"calibration complete. Saving to {self.calib_seed_file}...")
            _save_calibration_seed(results, self.calib_seed_file)
        return results

    def to_native_experiments(
        self, qobj: PulseQobj, native_config: NativeQobjConfig, /
    ) -> List[QuantifyExperiment]:
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

    def run_native(
        self,
        experiment: QuantifyExperiment,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        if self._experiment_delay > 0:
            time.sleep(self._experiment_delay)

        # Stop any running sequences.
        self._coordinator.stop()

        compiled_schedule = self._compiler.compile(
            schedule=experiment.schedule,
            config=self._compilation_config,
        )

        logger.log_Q1ASM_programs(compiled_schedule)
        logger.log_schedule(compiled_schedule)

        self._coordinator.prepare(compiled_schedule)
        self._coordinator.start()
        self._coordinator.wait_done(timeout_sec=10)
        results = self._coordinator.retrieve_acquisition()

        return QExperimentResult.from_xarray(results)

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
            cluster_mode=cluster_mode,
            stdout_log_level=25,
            file_log_level=25,
            cluster_ip=conf.ip_address,
            redis_url=self._redis_url,
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
    data = {
        "calibration_config": calibration.to_calibration_config().model_dump(
            mode="json"
        )
    }
    with open(file, "w") as seed_file:
        toml.dump(data, seed_file)


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
            int(idx): _QBLOX_CLUSTER_TYPE_MAP[conf.instrument_type]
            for idx, conf in conf.modules.items()
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
