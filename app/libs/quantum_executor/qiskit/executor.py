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
from typing import List, Type

import numpy as np
from numpy import typing as npt
from qiskit.result import Result as qiskitResult

from app.libs.qiskit.qobj import PulseQobj
from app.libs.quantum_executor.base.executor import QuantumExecutor
from app.libs.quantum_executor.base.experiment import NativeExperiment
from app.libs.quantum_executor.qiskit.experiment import QiskitDynamicsExperiment

from ...device_parameters import BackendConfig
from ..base.quantum_job import (
    MeasRet,
    QDataset,
    RepetitionsByAcquisitionsMatrix,
    get_experiment_name,
)
from ..base.quantum_job.dtos import NativeQobjConfig
from ..base.quantum_job.typing import QExperimentResult
from ..utils.logger import ExperimentLogger
from .backends.base import QiskitPulseBackend
from .backends.one_qubit import QiskitPulse1Q
from .backends.two_qubit import QiskitPulse2Q


class QiskitDynamicsExecutor(QuantumExecutor):
    def __init__(
        self,
        backend_config: BackendConfig,
        backend_cls: Type[QiskitPulseBackend] = QiskitPulse1Q,
        **kwargs,
    ):
        """Creates the QiskitDynamicsExecutor for the given backend class and key-word args

        Args:
            backend_config: the configuration of the backend
            backend_cls: the class of the backend
            kwargs: extra key-word args to pass to the backend on initialisation
        """
        super().__init__(backend_config=backend_config, **kwargs)
        self.backend = backend_cls(backend_config=backend_config, **kwargs)

    def recalibrate(self) -> None:
        pass

    def _run_native(
        self,
        experiment: NativeExperiment,
        /,
        *,
        native_config: NativeQobjConfig,
        logger: ExperimentLogger,
    ) -> QExperimentResult:
        meas_return = _QiskitDynMeasReturn.from_native_qobj_config(native_config)
        shots = native_config.shots
        job = self.backend.run(
            experiment.schedule, shots=shots, meas_return=meas_return
        )
        result: qiskitResult = job.result()
        data: npt.NDArray[np.floating] = result.data()["memory"]
        return _to_xarray(data=data, meas_return=meas_return)

    def _to_native_experiments(
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

    def close(self):
        pass

    @classmethod
    def new_one_qubit(cls, backend_config: BackendConfig, reset: bool = False):
        """Generates a new one-qubit qiskit-dynamics executor

        Args:
            backend_config: the backend configuration
            reset: whether to reset the backend

        Returns:
            an instance of this class that has one-qubit
        """
        # TODO: Use measurement level provided by the client request if discriminator is not provided
        instance = cls(
            backend_config=backend_config,
            backend_cls=QiskitPulse1Q,
            meas_level=1,
            meas_return="single",
        )

        if reset:
            # train the discriminators and update the backend config
            instance.backend_config.calibration_config.discriminators = (
                instance.backend.train_discriminator()
            )

        return instance

    @classmethod
    def new_two_qubit(cls, backend_config: BackendConfig, reset: bool = False):
        """Generates a new two-qubit qiskit-dynamics executor

        Args:
            backend_config: the backend configuration
            reset: whether to reset the backend

        Returns:
            an instance of this class that has two coupled qubits
        """
        instance = cls(
            backend_config=backend_config,
            backend_cls=QiskitPulse2Q,
            meas_level=1,
            meas_return="single",
        )

        if reset:
            # train the discriminators and update the backend config
            instance.backend_config.calibration_config.discriminators = (
                instance.backend.train_discriminator()
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
    data: npt.NDArray[np.floating], meas_return: _QiskitDynMeasReturn
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

    # no_of_acq_per_channel = number of shots if meas_return = 'single'  or 1 if meas_return = 'avg'.
    no_of_acq_channels = acquisitions.shape[1]

    # The index seems unnecessary
    # from https://quantify-os.org/docs/quantify-scheduler/v0.22.1/reference/acquisition_protocols.html#single-sideband-complex-integration

    return QDataset(
        data_vars={
            f"{i}": RepetitionsByAcquisitionsMatrix(
                data=np.expand_dims(acquisitions[:, i], 1),
                dims=["repetition", f"acq_index_{i}"],
            )
            for i in range(no_of_acq_channels)
        },
    )
