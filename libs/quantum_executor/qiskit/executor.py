# This code is part of Tergite
#
# (C) Stefan Hill (2024)
# (C) Martin Ahindura (2025)
# (C) Chalmers Next Labs (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
import enum
from pathlib import Path
from typing import List, Sequence, Union

import numpy as np
import qiskit.pulse as qiskit_pulse
from numpy import typing as npt
from qiskit.pulse import Schedule
from qiskit.result import Result as qiskitResult
from qiskit_dynamics import DynamicsBackend

from libs.qiskit.qobj import PulseQobj
from libs.quantum_executor.base.circuits import CircuitExperiment, QasmCircuit
from libs.quantum_executor.base.executor import QuantumExecutor
from libs.quantum_executor.base.experiment import NativeExperiment
from libs.quantum_executor.base.qasm import to_native_circuit
from libs.quantum_executor.qiskit.circuits import get_circuit_schedule
from libs.quantum_executor.qiskit.experiment import QiskitDynamicsExperiment

from ...device_parameters import BackendConfig
from ..base.quantum_job import MeasRet, QDataset, get_experiment_name
from ..base.quantum_job.dtos import NativeQobjConfig
from ..base.quantum_job.typing import QExperimentResult, RepetitionsByAcquisitionsMatrix
from ..utils.logger import ExperimentLogger
from .backends.one_qubit import get_one_qubit_backend, train_one_qubit_discriminator
from .backends.two_qubit import get_two_qubit_backend, train_two_qubit_discriminator


class QiskitDynamicsExecutor(QuantumExecutor):
    """Runs jobs on a qiskit-dynamics pulse simulator instead of real hardware"""

    def __init__(
        self,
        backend_config: BackendConfig,
        data_dir: Path,
        backend: DynamicsBackend,
    ):
        """Creates the QiskitDynamicsExecutor that simulates on the given backend

        Args:
            backend_config: the configuration of the backend
            data_dir: the directory where to save experiment data
            backend: the simulator
        """
        super().__init__(data_dir=data_dir, backend_config=backend_config)
        self.backend = backend

    def recalibrate(self, **kwargs) -> None:
        """Does nothing, as a simulator needs no recalibration"""
        pass

    def run_native(
        self,
        experiment: Union[NativeExperiment, CircuitExperiment[Schedule]],
        /,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        """Simulates the native experiment

        Args:
            experiment: the native experiment to run, of a qobj or of a circuit
            native_config: native config for the qobj
            logger: the logger for the given experiment

        Returns:
            xarray.Dataset of results, one data variable per acquisition channel
        """
        meas_return = _QiskitDynMeasReturn.from_native_qobj_config(native_config)
        shots = native_config.shots
        job = self.backend.run(
            experiment.schedule, shots=shots, meas_return=meas_return
        )
        result: qiskitResult = job.result()
        memory: npt.NDArray[np.floating] = result.data()["memory"]
        return _to_xarray(
            data=memory,
            meas_return=meas_return,
            slots=_get_acquired_slots(experiment.schedule),
        )

    def to_native_experiments(
        self, qobj: PulseQobj, native_config: NativeQobjConfig, /
    ) -> List[QiskitDynamicsExperiment]:
        """Constructs qiskit dynamics experiments from the PulseQobj instance

        Args:
            qobj: the Pulse qobject containing the experiments
            native_config: the native config for the qobj

        Returns:
            list of QiskitDynamicsExperiment's
        """
        native_experiments = [
            QiskitDynamicsExperiment.from_qobj_expt(
                name=get_experiment_name(expt.header.name, idx + 1),
                expt=expt,
                qobj_config=qobj.config,
                backend_config=self.backend_config,
            )
            for idx, expt in enumerate(qobj.experiments)
        ]
        return native_experiments

    def to_circuit_experiments(
        self, circuits: List[QasmCircuit], native_config: NativeQobjConfig, /
    ) -> List[CircuitExperiment[Schedule]]:
        """Compiles the circuits of a gate-level job to pulse schedules from the simulated
        device's calibration, each circuit qubit i being the i-th qubit by its number

        Args:
            circuits: the circuits of the job
            native_config: the native config for the job

        Returns:
            an experiment per circuit, each measurement acquired into the memory slot of
            its channel

        Raises:
            UnsupportedCircuitError: a circuit has a gate or qubits the simulator lacks
        """
        dt = self.backend_config.general_config.dt
        qubit_ids = self.backend_config.device_config.qubit_numbers
        experiments = []
        for idx, circuit in enumerate(circuits):
            native_circuit = to_native_circuit(circuit, dt=dt)
            schedule = get_circuit_schedule(native_circuit, self.backend_config)
            experiments.append(
                CircuitExperiment(
                    name=get_experiment_name(circuit.name, idx + 1),
                    schedule=schedule,
                    duration=schedule.duration * dt,
                    readout=native_circuit.to_readout(qubit_ids),
                )
            )
        return experiments

    def close(self):
        """Does nothing, as the simulator holds no resources to free"""
        pass

    @classmethod
    def new_one_qubit(
        cls, backend_config: BackendConfig, data_dir: Path, should_reset: bool = False
    ):
        """Generates a new one-qubit qiskit-dynamics executor

        Args:
            backend_config: the backend configuration
            data_dir: the directory where to save experiment data
            should_reset: whether to reset the backend

        Returns:
            an instance of this class that has one-qubit
        """
        # TODO: Use measurement level provided by the client request if discriminator is not provided
        instance = cls(
            backend_config=backend_config,
            data_dir=data_dir,
            backend=get_one_qubit_backend(
                backend_config, meas_level=1, meas_return="single"
            ),
        )

        if should_reset:
            # train the discriminators and update the backend config
            instance.backend_config.calibration_config.discriminators = (
                train_one_qubit_discriminator(instance.backend, backend_config)
            )

        return instance

    @classmethod
    def new_two_qubit(
        cls, backend_config: BackendConfig, data_dir: Path, should_reset: bool = False
    ):
        """Generates a new two-qubit qiskit-dynamics executor

        Args:
            backend_config: the backend configuration
            data_dir: the directory where to save experiment data
            should_reset: whether to reset the backend

        Returns:
            an instance of this class that has two coupled qubits
        """
        instance = cls(
            backend_config=backend_config,
            data_dir=data_dir,
            backend=get_two_qubit_backend(
                backend_config, meas_level=1, meas_return="single"
            ),
        )

        if should_reset:
            # train the discriminators and update the backend config
            instance.backend_config.calibration_config.discriminators = (
                train_two_qubit_discriminator(instance.backend)
            )
        return instance


class _QiskitDynMeasReturn(str, enum.Enum):
    """Enum for the meas returns of qiskit dynamics backend"""

    SINGLE = "single"
    AVG = "avg"

    @classmethod
    def from_native_qobj_config(cls, native_config: NativeQobjConfig):
        """Gets an instance of this class from the native config

        Args:
            native_config: the native qobj config from which to retrieve meas_return

        Returns:
            the _QiskitDynamicsMeasReturn instance
        """
        if native_config.meas_return == MeasRet.AVERAGED:
            return cls.AVG
        elif native_config.meas_return == MeasRet.APPENDED:
            return cls.SINGLE
        raise ValueError(f"unexpected meas_return: {native_config.meas_return}")


def _to_complex(
    real: npt.NDArray[np.floating], imaginary: npt.NDArray[np.floating]
) -> npt.NDArray[np.complexfloating]:
    """Converts numpy arrays of real and imaginary values to ndarray of complex values

    Args:
        real: the array of the real parts
        imaginary: the array of the imaginary parts

    Returns:
        the ndarray of the complex values
    """
    return (1j * imaginary) + real


def _to_xarray(
    data: npt.NDArray[np.floating],
    meas_return: _QiskitDynMeasReturn,
    slots: Sequence[int],
) -> QExperimentResult:
    """Constructs an xarray dataset from the given data and meas_return type

    When meas_return = ``avg``, data is of shape (N, 2) where N = measured channels
    i.e. ::

        array([[re1, im1], [re2, im2], ..., [reN, imN]])

    When meas_return = ``single``, data is of shape (M, N, 2) where N = measured channels and M = shots
    i.e. ::

        array([
            [
                [re1_1, im1_1], [re1_2, im1_2], ..., [re1_N, im1_N],
                [re2_1, im1_1], [re1_2, im1_2], ..., [re1_N, im1_N],
                                ...
                [reM_1, imM_1], [reM_2, imM_2], ..., [reM_N, imM_N],
            ]
        ])

    It returns an xarray.Dataset like ::

        <xarray.Dataset>
        Dimensions:    (repetition: M, acq_index_0: 1, acq_index_1: 1)
        Dimensions without coordinates: repetition, acq_index_0, acq_index_1
        Data variables:
            0   (repetition, acq_index_0) complex128 192B (0.001+0.627j) ... (0.001+0.627j)
            1   (repetition, acq_index_1) complex128 192B (0.001+0.627j) ... (0.001+0.627j)

    where each acquisition channel has its own 2-dimensional DataArray with
    row=shot number (or repetition) and column = iq value on that channel
    i.e. ::

        0: array([[re1_1 + j im1_1], ..., [reM_1 + j imM_1]])
        1: array([[re1_2 + j im1_2], ..., [reM_2 + j imM_2]])
        ...

    Note: ``DynamicsBackend.run`` only supports measurements at one time thus don't expect
    multiple measurements on the same acquisition_channel (or qubit)

    Args:
        data: the data to convert.
        meas_return: the _QiskitDynMeasReturn type
        slots: the memory slots the experiment acquired into, each a qubit's; the data has
            a column for every slot up to the last, so those of unmeasured qubits are left out

    Returns:
        the xarray.Dataset representation of the data of format

    """
    # More details about acquisitions can be found on the quantify scheduler docs site
    # https://quantify-os.org/docs/quantify-scheduler/v0.22.1/user/user_guide.html#acquisition-channel-and-acquisition-index

    if meas_return == _QiskitDynMeasReturn.AVG:
        # for meas_return avg, there is only one data point averaged across the shots
        # incoming data is, where N = measured channels, shape = (N, 2):
        # array([[re1, im1], [re2, im2], ..., [reN, imN]])

        # Convert to 1-dimensional array of complex data of shape (N,)
        # array([re1 + j im1, re2 + j im2, ..., reN + j imN])
        acquisitions = _to_complex(real=data[:, 0], imaginary=data[:, 1])

        # expand the dimensions of the complex data to shape (1, N)
        # to give a similar shape as the output from meas_return = 'single'
        acquisitions = np.expand_dims(acquisitions, 0)
    else:
        # incoming data is, where N = measured channels, and M = shots, shape = (M, N, 2):
        # array([
        #   [
        #       [re1_1, im1_1], [re1_2, im1_2], ..., [re1_N, im1_N],
        #       [re2_1, im1_1], [re1_2, im1_2], ..., [re1_N, im1_N],
        #                           ...
        #       [reM_1, imM_1], [reM_2, imM_2], ..., [reM_N, imM_N],
        #   ]
        # ])
        #

        # 2-dimensional array of complex data
        # Convert to 2-dimensional array of complex data of shape (M, N)
        # array([[re1_1 + j im1_1, ..., re1_N + j im1_N], ..., [reM_1 + j imM_1, ..., reM_N + j imM_N]])
        acquisitions = _to_complex(real=data[:, :, 0], imaginary=data[:, :, 1])

    # The index seems unnecessary
    # from https://quantify-os.org/docs/quantify-scheduler/v0.22.1/reference/acquisition_protocols.html#single-sideband-complex-integration

    return QDataset(
        data_vars={
            f"{i}": RepetitionsByAcquisitionsMatrix(
                data=np.expand_dims(acquisitions[:, i], 1),
                dims=["repetition", f"acq_index_{i}"],
            )
            for i in slots
        },
    )


def _get_acquired_slots(schedule: Schedule) -> List[int]:
    """Gets the memory slots the schedule acquires into, in order

    Args:
        schedule: the schedule of an experiment

    Returns:
        the indexes of the memory slots its acquisitions write
    """
    return sorted(
        {
            instruction.mem_slot.index
            for _, instruction in schedule.instructions
            if isinstance(instruction, qiskit_pulse.Acquire)
        }
    )
