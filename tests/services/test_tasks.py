# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Tests for the pure steps of the scheduler's tasks"""
import numpy as np
import pytest
import xarray as xr

from libs.quantum_executor.base.circuits import CircuitReadout, Measurement
from libs.quantum_executor.base.quantum_job.dtos import (
    MeasLvl,
    MeasRet,
    QuantumJob,
)
from services.scheduler.tasks import _get_memory


def test_integrated_results_are_the_iq_points_of_each_shot():
    job = _get_job(MeasLvl.INTEGRATED, data=np.array([[1 + 2j], [3 + 4j]]))

    assert _get_memory(job, calibration=None) == [[[(1.0, 2.0)], [(3.0, 4.0)]]]


def test_raw_results_are_refused():
    job = _get_job(MeasLvl.RAW, data=np.array([[1 + 2j]]))

    with pytest.raises(NotImplementedError, match="meas_level"):
        _get_memory(job, calibration=None)


def _get_job(meas_level: MeasLvl, data: np.ndarray) -> QuantumJob:
    """Gets a job of one experiment, which measures one qubit on its one channel"""
    dataset = xr.Dataset({"0": xr.DataArray(data, dims=("repetition", "acq"))})
    return QuantumJob(
        tuid="t",
        meas_return=MeasRet.APPENDED,
        meas_level=meas_level,
        meas_return_cols=1,
        n_qubits=1,
        readouts=[CircuitReadout("exp", 1, (Measurement(12, 0),))],
        raw_results={"exp~0": dataset},
    )
