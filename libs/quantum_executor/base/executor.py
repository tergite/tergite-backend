# This code is part of Tergite
#
# (C) Stefan Hill (2024)
# (C) Martin Ahindura (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import abc
import pickle
from dataclasses import dataclass
from pathlib import Path
from traceback import format_exc
from typing import Dict, List, Optional, Tuple

from quantify_core.data.handling import create_exp_folder, gen_tuid
from quantify_core.data.types import TUID

from libs.device_parameters import BackendConfig, DeviceCalibration
from libs.quantum_executor.base.circuits import (
    CircuitExperiment,
    CircuitJobParams,
    QasmCircuit,
)
from libs.quantum_executor.base.pulse_limits import (
    check_pulse_limits,
    get_calibrated_frame_frequencies,
)
from libs.quantum_executor.base.qasm import to_native_circuit
from libs.quantum_executor.base.quantum_job import (
    save_job_in_hdf5,
)
from libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig, QuantumJob
from libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from libs.quantum_executor.utils.logger import ExperimentLogger
from utils.exc import UnsupportedCircuitError


class QuantumExecutor(abc.ABC):
    """The state a device's jobs run with, and the hooks that preprocess_circuits and
    run_job call to compile and run them natively"""

    def __init__(
        self,
        data_dir: Path,
        backend_config: Optional[BackendConfig] = None,
        **kwargs,
    ):
        # where each experiment's folder and log go
        self.data_dir = Path(data_dir)
        self.backend_config = backend_config

    @abc.abstractmethod
    def to_circuit_experiments(
        self, circuits: List[QasmCircuit], native_config: NativeQobjConfig, /
    ) -> List[CircuitExperiment]:
        """Compiles the OpenQASM 3 circuits of a gate-level job, with the device's
        calibration, to experiments run_native runs

        Args:
            circuits: the circuits of the job
            native_config: the native config for the job

        Returns:
            an experiment per circuit, in the circuits' order, named as
            get_experiment_name names them
        """
        pass

    @abc.abstractmethod
    def run_native(
        self,
        experiment: CircuitExperiment,
        /,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        """Runs an experiment that to_circuit_experiments compiled

        It returns an xarray.Dataset like ::

            <xarray.Dataset>
            Dimensions:    (repetition: M, acq_index_0: 1, acq_index_1: 2)
            Dimensions without coordinates: repetition, acq_index_0, acq_index_1
            Data variables:
                0   (repetition, acq_index_0) complex128 192B (0.001+0.627j) ... (0.001+0.627j)
                1   (repetition, acq_index_1) complex128 192B (0.1+0.6j, 0.1+0.6j,) ... (0.1+0.6j, 0.1+0.6j)

        where each acquisition channel has its own 2-dimensional DataArray with
        row=shot number (or repetition),
        column = iq values on for each measurement on that channel in the given shot in given schedule
        e.g. if a qubit has two measurements in the circuit, each shot will have two iq pairs
        ::

            0: array([[re1_1 + j im1_1], ..., [reM_1 + j imM_1]])
            1: array([
                [re1_2a + j im1a_2a, re1_2b + j im1b_2b],
                ...
                [reM_2a + j imM_2a, reM_2b + j imM_2b],
              ])
            ...

        Args:
            experiment: the experiment of a circuit to run
            native_config: native config for the job
            logger: the logger for the given experiment which logs data in a specific folder

        Returns:
            xarray.Dataset of results
        """
        pass

    @abc.abstractmethod
    def recalibrate(self, **kwargs) -> DeviceCalibration | None:
        """Recalibrates the executor

        Returns:
            the device calibration after recalibration, or None if this executor
            cannot recalibrate
        """

    def add_pulse_calibration(
        self, calibration: DeviceCalibration
    ) -> DeviceCalibration:
        """Adds to the calibration the frames the device's pulse-level jobs may play on and
        its gates' defcals, which MSS publishes; an executor of a device that takes no
        pulse-level jobs adds none

        Args:
            calibration: the device's calibration, of this executor's backend config

        Returns:
            the calibration, with the frames and defcals where the device has them
        """
        return calibration

    @abc.abstractmethod
    def close(self):
        """Frees the resources the executor holds, e.g. its connections to instruments"""
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()


def preprocess_circuits(
    executor: QuantumExecutor,
    params: CircuitJobParams,
    job_id: str,
    results_folder: Path,
) -> Tuple[float, str]:
    """Compiles a gate-level job's circuits on the executor, but does not run them yet

    Args:
        executor: the executor the job is to run on
        params: the circuits of the job and how to measure them
        job_id: the ID of the job
        results_folder: the folder to store the experiment resulting files

    Returns:
        tuple of the duration and the path to the pickle file containing the experiments
        metadata

    Raises:
        UnsupportedCircuitError: circuits {names} measure no qubit
        UnsupportedCircuitError: a pulse-level block beyond the device's limits; see
            pulse_limits.check_pulse_limits
    """
    native_config = NativeQobjConfig.from_circuit_job_params(
        params, n_qubits=executor.backend_config.general_config.num_qubits
    )
    expt_metadata = CircuitExptMetadata(
        native_config=native_config, tuid=gen_tuid(), params=params
    )
    tuid = expt_metadata.tuid
    create_exp_folder(tuid=tuid, name=expt_metadata.tag, datadir=executor.data_dir)
    logger = ExperimentLogger(tuid, datadir=executor.data_dir)
    logger.info(
        f"Preprocessing gate-level job for tuid: {tuid} (not the same as job_id)"
    )

    try:
        # checked again here, where the job runs, whatever way its file came
        dt = executor.backend_config.general_config.dt
        max_qubits = len(executor.backend_config.device_config.qubit_numbers)
        max_clbits = executor.backend_config.general_config.max_clbits
        check_pulse_limits(
            [
                to_native_circuit(
                    circuit, dt=dt, max_qubits=max_qubits, max_clbits=max_clbits
                )
                for circuit in params.circuits
            ],
            shots=params.shots,
            limits=executor.backend_config.pulse_limits,
            frame_frequencies=get_calibrated_frame_frequencies(executor.backend_config),
            dt=dt,
        )
        experiments = executor.to_circuit_experiments(params.circuits, native_config)
        # an experiment without acquisitions has no results to save, which would leave
        # every other circuit's results without their own on reading them back
        unmeasured = [
            circuit.name
            for circuit, expt in zip(params.circuits, experiments)
            if not expt.readout.measurements
        ]
        if unmeasured:
            raise UnsupportedCircuitError(
                f"circuits {', '.join(unmeasured)} measure no qubit"
            )
        total_duration = sum(expt.duration for expt in experiments)

        results_file_path = _get_expt_metadata_file(job_id, folder=results_folder)
        with open(results_file_path, "wb") as file:
            pickle.dump(expt_metadata.to_dict(), file)

        logger.info(f"Compiled {len(experiments)} circuits.")
        return total_duration, str(results_file_path)
    except Exception as e:
        logger.error(f"\nFailed job: {job_id}, tuid: {tuid}\n{format_exc()}")
        raise e


def run_job(executor: QuantumExecutor, job_id: str, inputs_folder: Path) -> str:
    """Runs the preprocessed job's experiments on the executor, and returns the results file path

    Args:
        executor: the executor the job was preprocessed for
        job_id: the ID of the job
        inputs_folder: the path to the folder where the input files for this run can be found

    Returns:
        the path to the results obtained after measurement
    """
    preprocessed_file = _get_expt_metadata_file(job_id, folder=inputs_folder)
    logger: Optional[ExperimentLogger] = None
    try:
        expt_metadata = _load_expt_metadata(preprocessed_file)
        tuid = expt_metadata.tuid
        experiment_folder = Path(
            create_exp_folder(
                tuid=tuid, name=expt_metadata.tag, datadir=executor.data_dir
            )
        )
        logger = ExperimentLogger(tuid, datadir=executor.data_dir)
        logger.info(f"Starting job: {tuid}")
        logger.info(f"Running experiments for job id: {job_id}")

        native_expts, raw_results = _run_experiments(
            executor, expt_metadata, logger=logger
        )
        results_file_path = _save_results(
            job_id, expt_metadata, native_expts, raw_results, folder=experiment_folder
        )
        preprocessed_file.unlink(missing_ok=True)

        logger.info(f"Stored measurement data at {results_file_path}")
        logger.info(f"Completed {job_id if job_id else 'local job'} with tuid {tuid}.")
    except Exception as e:
        if logger:
            logger.error(f"\nFailed job: {job_id}\n{format_exc()}")
        raise e

    return str(results_file_path)


@dataclass(frozen=True)
class CircuitExptMetadata:
    """Metadata on the experiments of a gate-level job

    This is passed between processes doing preprocessing and those doing execution
    """

    native_config: NativeQobjConfig
    tuid: TUID
    params: CircuitJobParams
    # the name of the experiments' folder, beside its tuid
    tag: str = ""

    def to_quantum_job(
        self,
        job_id: str,
        raw_results: Dict[str, QExperimentResult],
        experiments: List[CircuitExperiment],
    ) -> QuantumJob:
        """Gets the job these experiments ran for, with their results

        Args:
            job_id: the ID of the job
            raw_results: each experiment's results, by its name
            experiments: the experiments, whose readouts postprocessing reads

        Returns:
            the quantum job, with its circuits' readouts
        """
        return QuantumJob(
            job_id=job_id,
            tuid=self.tuid,
            meas_return=self.native_config.meas_return,
            meas_return_cols=self.native_config.meas_return_cols,
            meas_level=self.native_config.meas_level,
            n_qubits=self.native_config.n_qubits,
            readouts=[expt.readout for expt in experiments],
            raw_results=raw_results,
            shots=self.native_config.shots,
        )

    def to_dict(self) -> dict:
        """Converts this experiment metadata to a picklable dict

        Returns:
            the dict of this metadata
        """
        return {
            "tuid": self.tuid,
            "params": self.params.model_dump(),
            "native_config": self.native_config.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict) -> "CircuitExptMetadata":
        """Gets an instance of this class from a dict that to_dict made

        Args:
            value: the dict to be converted

        Returns:
            the instance of this class
        """
        return cls(
            tuid=value["tuid"],
            params=CircuitJobParams.model_validate(value["params"]),
            native_config=NativeQobjConfig.from_dict(value["native_config"]),
        )


def _get_expt_metadata_file(job_id: str, folder: Path) -> Path:
    """Gets the path of the file in the folder that the job's experiments' metadata is
    pickled to"""
    return folder / f"{job_id}-expt-metadata.pickle"


def _load_expt_metadata(file: Path) -> CircuitExptMetadata:
    """Loads the metadata of the experiments that preprocessing pickled to the file"""
    with open(file, "rb") as metadata_file:
        return CircuitExptMetadata.from_dict(pickle.load(metadata_file))


def _run_experiments(
    executor: QuantumExecutor,
    expt_metadata: CircuitExptMetadata,
    logger: ExperimentLogger,
) -> Tuple[List[CircuitExperiment], Dict[str, QExperimentResult]]:
    """Runs the job's experiments natively

    Returns:
        the experiments, and each one's result keyed by its name
    """
    native_config = expt_metadata.native_config
    # compiled again rather than kept from preprocessing, as pickling them broke the schedules
    native_expts = executor.to_circuit_experiments(
        expt_metadata.params.circuits, native_config
    )
    raw_results = {
        expt.name: executor.run_native(expt, native_config=native_config, logger=logger)
        for expt in native_expts
    }
    return native_expts, raw_results


def _save_results(
    job_id: str,
    expt_metadata: CircuitExptMetadata,
    native_expts: List[CircuitExperiment],
    raw_results: Dict[str, QExperimentResult],
    folder: Path,
) -> Path:
    """Saves the job and its experiments' results in an HDF5 file in the folder

    Returns:
        the file's path
    """
    job = expt_metadata.to_quantum_job(job_id, raw_results, native_expts)
    results_file_path = folder / f"{job_id}.hdf5"
    save_job_in_hdf5(job, results_file_path)
    return results_file_path
