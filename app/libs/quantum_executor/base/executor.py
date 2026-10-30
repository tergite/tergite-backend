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
from datetime import datetime
from pathlib import Path
from traceback import format_exc
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.libs.device_parameters import BackendConfig, DeviceCalibration
from app.libs.qiskit.qobj import PulseQobj
from app.libs.quantum_executor.base.experiment import NativeExperiment
from app.libs.quantum_executor.base.quantum_job import (
    save_job_in_hdf5,
)
from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig, QuantumJob
from app.libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from app.libs.quantum_executor.utils.logger import ExperimentLogger
from app.utils.compat import TUID, create_exp_folder, gen_tuid


class QuantumExecutor(abc.ABC):
    """The state a device's jobs run with, and the hooks that preprocess_qobj and run_job
    call to compile and run them natively"""

    def __init__(
        self,
        data_dir: Path,
        hardware_map: Optional[Dict[str, Tuple[str, str]]] = None,
        backend_config: Optional[BackendConfig] = None,
        **kwargs,
    ):
        # where each experiment's folder and log go
        self.data_dir = Path(data_dir)
        self.hardware_map = hardware_map
        self.backend_config = backend_config

    @abc.abstractmethod
    def to_native_experiments(
        self, qobj: PulseQobj, native_config: NativeQobjConfig, /
    ) -> List[NativeExperiment]:
        """Constructs native experiments from the PulseQobj instance

        Args:
            qobj: the Pulse qobject containing the experiments
            native_config: the native config for the qobj

        Returns:
            list of NativeExperiment's
        """
        pass

    @abc.abstractmethod
    def run_native(
        self,
        experiment: NativeExperiment,
        /,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        """Runs the native experiments after they have been compiled from OpenPulse Qobjects

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
            experiment: the native experiment to run
            native_config: native config for the qobj
            logger: the logger for the given experiment which logs data in a specific folder

        Returns:
            xarray.Dataset of results
        """
        pass

    @abc.abstractmethod
    def recalibrate(self, **kwargs) -> DeviceCalibration | None:
        """Recalibrates the executor"""

    @abc.abstractmethod
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()


def preprocess_qobj(
    executor: QuantumExecutor, qobj: PulseQobj, job_id: str, results_folder: Path
) -> Tuple[float, str]:
    """Prepares a given qobj for execution on the executor but does not execute it yet

    Args:
        executor: the executor the job is to run on
        qobj: the Quantum object that is to be preprocessed
        job_id: the ID of the job
        results_folder: the folder to store the experiment resulting files

    Returns:
        tuple of the duration and the path to the pickle file containing the native experiments metadata
    """
    tuid = gen_tuid()
    qobj_header = qobj.header.to_dict()
    qobj_tag = qobj_header.get("tag", "")
    # create the logger folder where to log messages etc.
    create_exp_folder(tuid=tuid, name=qobj_tag, datadir=executor.data_dir)
    logger = ExperimentLogger(tuid, datadir=executor.data_dir)
    logger.info(f"Preprocessing job for tuid: {tuid} (not the same as job_id)")

    try:
        # unwrap pulse library
        qobj.config.pulse_library = {
            i.name: np.asarray(i.samples) for i in qobj.config.pulse_library
        }

        # translate qobj experiments to quantify schedules
        logger.info(f"Started compilation for job id: {job_id} at {datetime.now()}")
        native_config = NativeQobjConfig.from_pulse_qobj_config(qobj.config)
        native_expts = executor.to_native_experiments(qobj, native_config)
        total_duration = sum([expt.duration for expt in native_expts])

        data = NativeExptMetadata(
            native_config=native_config,
            tuid=tuid,
            qobj=qobj,
            qobj_tag=qobj_tag,
        )

        results_file_path = _get_preprocessed_expt_file(job_id, folder=results_folder)
        with open(results_file_path, "wb") as file:
            pickle.dump(data.to_dict(), file)

        logger.info(f"Translated to {len(native_expts)} native experiments.")
        return total_duration, str(results_file_path)
    except Exception as e:
        # log exceptions
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
    preprocessed_file = _get_preprocessed_expt_file(job_id, folder=inputs_folder)
    logger: Optional[ExperimentLogger] = None
    try:
        expt_metadata = _load_expt_metadata(preprocessed_file)
        tuid = expt_metadata.tuid
        experiment_folder = Path(
            create_exp_folder(
                tuid=tuid, name=expt_metadata.qobj_tag, datadir=executor.data_dir
            )
        )
        logger = ExperimentLogger(tuid, datadir=executor.data_dir)
        logger.info(f"Starting job: {tuid}")
        logger.info(f"Running experiments for job id: {job_id}")

        raw_results = _run_experiments(executor, expt_metadata, logger=logger)
        results_file_path = _save_results(
            job_id, expt_metadata, raw_results, folder=experiment_folder
        )
        preprocessed_file.unlink(missing_ok=True)

        logger.info(f"Stored measurement data at {results_file_path}")
        logger.info(f"Completed {job_id if job_id else 'local job'} with tuid {tuid}.")
    except Exception as e:
        if logger:
            # record exceptions
            logger.error(f"\nFailed job: {job_id}\n{format_exc()}")
        raise e

    return str(results_file_path)


@dataclass(frozen=True)
class NativeExptMetadata:
    """Metadata on the experiments

    This is passed between processes doing preprocessing and those doing execution
    """

    native_config: NativeQobjConfig
    tuid: TUID
    qobj: PulseQobj
    qobj_tag: str

    def to_dict(self) -> dict:
        """Converts this experiment metadata to JSON serializable dict"""
        return {
            "tuid": self.tuid,
            "qobj": self.qobj.to_dict(),
            "native_config": self.native_config.to_dict(),
            "qobj_tag": self.qobj_tag,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "NativeExptMetadata":
        """Gets an instance of this class from a dict

        Args:
            value: the dict to be converted

        Returns:
            the instance of this class
        """
        kwargs = {
            **value,
            "qobj": PulseQobj.from_dict(value["qobj"]),
            "native_config": NativeQobjConfig.from_dict(value["native_config"]),
        }
        return cls(**kwargs)


def _get_preprocessed_expt_file(job_id: str, folder: Path) -> Path:
    """Gets the path to the file containing the preprocessed experiments

    Args:
        job_id: the unique identifier of the job
        folder: the folder containing the preprocessed experiment files

    Returns:
        the path to the file
    """
    return folder / f"{job_id}-expt-metadata.json"


def _load_expt_metadata(file: Path) -> "NativeExptMetadata":
    """Loads the metadata of the experiments that preprocessing pickled to the file"""
    with open(file, "rb") as metadata_file:
        return NativeExptMetadata.from_dict(pickle.load(metadata_file))


def _run_experiments(
    executor: QuantumExecutor,
    expt_metadata: "NativeExptMetadata",
    logger: ExperimentLogger,
) -> Dict[str, QExperimentResult]:
    """Runs the job's experiments natively, each result keyed by its experiment's name"""
    native_config = expt_metadata.native_config
    # we recompile these because pickling them seems to break the schedules
    # FIXME: If compilation is expensive, removing this duplication is a good place to start
    native_expts = executor.to_native_experiments(expt_metadata.qobj, native_config)
    return {
        expt.header.name: executor.run_native(
            expt, native_config=native_config, logger=logger
        )
        for expt in native_expts
    }


def _save_results(
    job_id: str,
    expt_metadata: "NativeExptMetadata",
    raw_results: Dict[str, QExperimentResult],
    folder: Path,
) -> Path:
    """Saves the job and its experiments' results in an HDF5 file in the folder

    Returns:
        the file's path
    """
    native_config = expt_metadata.native_config
    job = QuantumJob(
        job_id=job_id,
        tuid=expt_metadata.tuid,
        meas_return=native_config.meas_return,
        meas_return_cols=native_config.meas_return_cols,
        meas_level=native_config.meas_level,
        n_qubits=native_config.n_qubits,
        memory_slot_size=expt_metadata.qobj.config.memory_slot_size,
        qobj=expt_metadata.qobj,
        raw_results=raw_results,
    )
    results_file_path = folder / f"{job_id}.hdf5"
    save_job_in_hdf5(job, results_file_path)
    return results_file_path
