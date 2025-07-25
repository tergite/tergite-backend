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
from datetime import datetime
from pathlib import Path
from traceback import format_exc
from typing import Dict, List, Optional, Tuple, TypedDict

import numpy as np
from qiskit.qobj import PulseQobj
from quantify_core.data import handling as dh
from quantify_core.data.handling import create_exp_folder, gen_tuid
from quantify_core.data.types import TUID

import settings
from app.libs.quantum_executor.base.experiment import NativeExperiment
from app.libs.quantum_executor.base.quantum_job import (
    save_job_in_hdf5,
    to_native_qobj_config,
)
from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig, QuantumJob
from app.libs.quantum_executor.base.quantum_job.typing import QExperimentResult
from app.libs.quantum_executor.utils.logger import ExperimentLogger


class QuantumExecutor(abc.ABC):
    def __init__(
        self,
        hardware_map: Optional[Dict[str, Tuple[str, str]]] = None,
    ):
        dh.set_datadir(settings.EXECUTOR_DATA_DIR)
        self.hardware_map = hardware_map

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

    def preprocess(self, qobj: PulseQobj, job_id: str = None) -> Tuple[float, Path]:
        """Prepares a given qobj for execution but does not execute it yet

        Args:
            qobj: the Quantum object that is to be preprocessed
            job_id: the ID of the job

        Returns:
            tuple of the duration and the path to the pickle file containing the native experiments metadata
        """
        tuid = gen_tuid()
        qobj_header = qobj.header.to_dict()
        qobj_tag = qobj_header.get("tag", "")
        experiment_folder = Path(create_exp_folder(tuid=tuid, name=qobj_tag))
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
            # TODO: get total_duration from the native experiments

            filename = (
                "expt-metadata.pkl" if job_id is None else f"{job_id}-expt-metadata.pkl"
            )
            results_file_path = experiment_folder / filename

            data: NativeExperimentMetadata = {
                "native_experiments": native_expts,
                "native_config": native_config,
                "tuid": tuid,
                "duration": total_duration,
                "qobj": qobj,
            }
            with open(results_file_path, "wb") as file:
                pickle.dump(data, file, protocol=5)

            logger.info(f"Translated to {len(native_expts)} native experiments.")
            return total_duration, results_file_path
        except Exception as e:
            # log exceptions
            logger.error(f"\nFailed job: {job_id}, tuid: {tuid}\n{format_exc()}")
            raise e

    def run(self, native_expt_file: Path, job_id: str = None) -> Path:
        """Runs the experiments and returns the results file path

        Args:
            native_expt_file: the path to the pickle file containing the experiment data
            job_id: the ID of the job

        Returns:
            the path to the results obtained after measurement
        """
        logger: Optional[ExperimentLogger] = None
        try:
            with open(native_expt_file, "rb") as file:
                expt_metadata: NativeExperimentMetadata = pickle.load(file)

            tuid = expt_metadata["tuid"]
            native_config = expt_metadata["native_config"]
            native_expts = expt_metadata["native_experiments"]
            qobj = expt_metadata["qobj"]

            experiment_folder = native_expt_file.parent
            logger = ExperimentLogger(tuid)
            logger.info(f"Starting job: {tuid}")

            logger.info(f"Running experiments for job id: {job_id}")
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

            filename = "measurement.hdf5" if job_id is None else f"{job_id}.hdf5"
            results_file_path = experiment_folder / filename
            save_job_in_hdf5(job, results_file_path)

            # cleanup
            native_expt_file.unlink(missing_ok=True)

            logger.info(f"Stored measurement data at {results_file_path}")
            logger.info(
                f"Completed {job_id if job_id else 'local job'} with tuid {tuid}."
            )
        except Exception as e:
            if logger:
                # record exceptions
                logger.error(f"\nFailed job: {job_id}, tuid: {tuid}\n{format_exc()}")
            raise e

        return results_file_path

    @abc.abstractmethod
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()


class NativeExperimentMetadata(TypedDict):
    """Metadata on the experiments

    This is passed between processes doing preprocessing and those doing execution
    """

    native_experiments: List[NativeExperiment]
    native_config: NativeQobjConfig
    tuid: TUID
    duration: float
    qobj: PulseQobj
