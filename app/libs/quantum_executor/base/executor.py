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
    to_native_qobj_config,
)
from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig, QuantumJob
from app.libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from app.libs.quantum_executor.utils.logger import ExperimentLogger
from app.utils.compat import TUID, create_exp_folder, gen_tuid
from settings import PREPROCESSED_JOB_POOL


class QuantumExecutor(abc.ABC):
    def __init__(
        self,
        hardware_map: Optional[Dict[str, Tuple[str, str]]] = None,
        backend_config: Optional[BackendConfig] = None,
        **kwargs,
    ):
        self.hardware_map = hardware_map
        self.backend_config = backend_config

    @abc.abstractmethod
    def _to_native_experiments(
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
    def _run_native(
        self,
        experiment: NativeExperiment,
        /,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        """Runs the native experiments after they have been compiled from OpenPulse Qobjects

        This method is internally called by the public run() method
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

    def preprocess(
        self, qobj: PulseQobj, job_id: str, results_folder: Path = PREPROCESSED_JOB_POOL
    ) -> Tuple[float, str]:
        """Prepares a given qobj for execution but does not execute it yet

        Args:
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
        create_exp_folder(tuid=tuid, name=qobj_tag)
        logger = ExperimentLogger(tuid)
        logger.info(f"Preprocessing job for tuid: {tuid} (not the same as job_id)")

        try:
            # unwrap pulse library
            qobj.config.pulse_library = {
                i.name: np.asarray(i.samples) for i in qobj.config.pulse_library
            }

            # translate qobj experiments to quantify schedules
            logger.info(f"Started compilation for job id: {job_id} at {datetime.now()}")
            native_config = to_native_qobj_config(qobj.config)
            native_expts = self._to_native_experiments(qobj, native_config)
            total_duration = sum([expt.duration for expt in native_expts])

            data = NativeExptMetadata(
                native_config=native_config,
                tuid=tuid,
                qobj=qobj,
                qobj_tag=qobj_tag,
            )

            results_file_path = _get_preprocessed_expt_file(
                job_id, folder=results_folder
            )
            with open(results_file_path, "wb") as file:
                pickle.dump(data.to_dict(), file)

            logger.info(f"Translated to {len(native_expts)} native experiments.")
            return total_duration, str(results_file_path)
        except Exception as e:
            # log exceptions
            logger.error(f"\nFailed job: {job_id}, tuid: {tuid}\n{format_exc()}")
            raise e

    @abc.abstractmethod
    def recalibrate(self, **kwargs) -> DeviceCalibration | None:
        """Recalibrates the executor"""

    def run(self, job_id: str, inputs_folder: Path = PREPROCESSED_JOB_POOL) -> str:
        """Runs the experiments and returns the results file path

        Args:
            job_id: the ID of the job
            inputs_folder: the path to the folder where the input files for this run can be found

        Returns:
            the path to the results obtained after measurement
        """
        logger: Optional[ExperimentLogger] = None
        try:
            preprocessed_file = _get_preprocessed_expt_file(
                job_id, folder=inputs_folder
            )
            with open(preprocessed_file, "rb") as file:
                expt_metadata_dict = pickle.load(file)
                expt_metadata = NativeExptMetadata.from_dict(expt_metadata_dict)

            tuid = expt_metadata.tuid
            qobj = expt_metadata.qobj
            native_config = expt_metadata.native_config
            qobj_tag = expt_metadata.qobj_tag

            experiment_folder = Path(create_exp_folder(tuid=tuid, name=qobj_tag))
            logger = ExperimentLogger(tuid)
            logger.info(f"Starting job: {tuid}")

            logger.info(f"Running experiments for job id: {job_id}")

            # we recompile these because pickling them seems to break the schedules
            # FIXME: If compilation is expensive, removing this duplication is a good place to start
            native_expts = self._to_native_experiments(qobj, native_config)
            experiment_results = {
                expt.header.name: self._run_native(
                    expt, native_config=native_config, logger=logger
                )
                for expt in native_expts
            }

            job = QuantumJob(
                job_id=job_id,
                tuid=tuid,
                meas_return=native_config.meas_return,
                meas_return_cols=native_config.meas_return_cols,
                meas_level=native_config.meas_level,
                n_qubits=native_config.n_qubits,
                memory_slot_size=qobj.config.memory_slot_size,
                qobj=qobj,
                raw_results=experiment_results,
            )

            results_file_path = experiment_folder / f"{job_id}.hdf5"
            save_job_in_hdf5(job, results_file_path)

            # cleanup
            preprocessed_file.unlink(missing_ok=True)

            logger.info(f"Stored measurement data at {results_file_path}")
            logger.info(
                f"Completed {job_id if job_id else 'local job'} with tuid {tuid}."
            )
        except Exception as e:
            if logger:
                # record exceptions
                logger.error(f"\nFailed job: {job_id}\n{format_exc()}")
            raise e

        return str(results_file_path)

    @abc.abstractmethod
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()


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


def _get_preprocessed_expt_file(
    job_id: str, folder: Path = PREPROCESSED_JOB_POOL
) -> Path:
    """Gets the path to the file containing the preprocessed experiments

    Args:
        job_id: the unique identifier of the job
        folder: the folder containing the preprocessed experiment files

    Returns:
        the path to the file
    """
    return folder / f"{job_id}-expt-metadata.json"
