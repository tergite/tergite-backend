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
"""Tests of the memory of gate-level jobs, from their acquisitions and readouts"""

from typing import Dict, List

import numpy as np
import pytest
import xarray as xr

from libs.quantum_executor.base.circuits import CircuitReadout, Measurement
from libs.quantum_executor.base.quantum_job import (
    _get_shot_iq_points,
    _to_iq_point,
    discriminate_circuit_results,
    get_circuit_iq_memory,
    read_job_from_hdf5,
    save_job_in_hdf5,
)
from libs.quantum_executor.base.quantum_job.dtos import MeasLvl, MeasRet, QuantumJob
from libs.quantum_executor.base.quantum_job.typing import QDataset

_SHOTS = 3


@pytest.mark.parametrize(
    "measurements, states_by_channel, num_clbits, expected",
    [
        ([Measurement(12, 0)], [1], 1, "0x1"),
        ([Measurement(13, 0)], [1], 2, "0x1"),
        ([Measurement(12, 1), Measurement(13, 0)], [1, 0], 2, "0x2"),
        ([Measurement(12, 1), Measurement(13, 0)], [0, 1], 2, "0x1"),
        ([Measurement(12, 2)], [1], 3, "0x4"),
        ([Measurement(12, 0), Measurement(12, 0)], [1, 0], 1, "0x0"),
    ],
)
def test_each_measurement_lands_on_its_clbit(
    measurements, states_by_channel, num_clbits, expected
):
    readout = CircuitReadout("test", num_clbits, tuple(measurements))
    dataset = _get_dataset({ch: [s] * _SHOTS for ch, s in enumerate(states_by_channel)})
    job = _get_job(MeasLvl.DISCRIMINATED, readouts=[readout], datasets=[dataset])

    got = discriminate_circuit_results(job, discriminator=_get_states)

    assert got == [[expected] * _SHOTS]


def test_discriminator_gets_each_measurements_qubit():
    readout = CircuitReadout("test", 2, (Measurement(14, 1), Measurement(12, 0)))
    dataset = _get_dataset({0: [0] * _SHOTS, 1: [0] * _SHOTS})
    job = _get_job(MeasLvl.DISCRIMINATED, readouts=[readout], datasets=[dataset])
    qubit_ids = []

    def discriminator(qubit_id, iq_points):
        qubit_ids.append(qubit_id)
        return _get_states(qubit_id, iq_points)

    discriminate_circuit_results(job, discriminator=discriminator)

    assert qubit_ids == [14, 12]


def test_iq_memory_lists_each_shots_points_by_clbit():
    readout = CircuitReadout("test", 3, (Measurement(12, 2), Measurement(13, 0)))
    dataset = _get_dataset({0: [1 + 2j, 3 + 4j], 1: [5 + 6j, 7 + 8j]})
    job = _get_job(MeasLvl.INTEGRATED, readouts=[readout], datasets=[dataset])

    got = get_circuit_iq_memory(job)

    assert got == [[[(5.0, 6.0), (1.0, 2.0)], [(7.0, 8.0), (3.0, 4.0)]]]


def test_averaged_iq_memory_has_one_point_per_clbit():
    readout = CircuitReadout("test", 2, (Measurement(12, 1), Measurement(13, 0)))
    dataset = QDataset(
        {
            "0": xr.DataArray(np.array([1 + 2j]), dims=("acq_index_0",)),
            "1": xr.DataArray(np.array([3 + 4j]), dims=("acq_index_1",)),
        }
    )
    job = _get_job(
        MeasLvl.INTEGRATED,
        readouts=[readout],
        datasets=[dataset],
        meas_return=MeasRet.AVERAGED,
    )

    got = get_circuit_iq_memory(job)

    assert got == [[[(3.0, 4.0), (1.0, 2.0)]]]


def test_a_shot_lists_each_channels_iq_points_in_turn():
    dataset = QDataset(
        {
            "0": xr.DataArray(
                np.array([[1 + 2j, 3 + 4j], [5 + 6j, 7 + 8j]]),
                dims=("repetition", "acq"),
            ),
            "1": xr.DataArray(
                np.array([[9j, 11j], [10j, 12j]]), dims=("repetition", "acq")
            ),
        }
    )

    points = _get_shot_iq_points(dataset, channel_ids=[1, 0], shot=1)

    assert points == [(0.0, 10.0), (0.0, 12.0), (5.0, 6.0), (7.0, 8.0)]


@pytest.mark.parametrize(
    "value, expected",
    [
        (np.complex128(1 + 2j), (1.0, 2.0)),
        (np.complex128(complex(np.nan, 1)), (0.0, 0.0)),
    ],
)
def test_iq_point_is_zero_unless_finite(value, expected):
    assert _to_iq_point(value) == expected


def test_readouts_must_match_the_experiments():
    readout = CircuitReadout("test", 1, (Measurement(12, 0),))
    dataset = _get_dataset({0: [0] * _SHOTS})
    job = _get_job(
        MeasLvl.DISCRIMINATED, readouts=[readout, readout], datasets=[dataset]
    )

    with pytest.raises(ValueError, match="2 readouts for 1 experiment results"):
        discriminate_circuit_results(job, discriminator=_get_states)


@pytest.mark.parametrize("meas_return", [MeasRet.APPENDED, MeasRet.AVERAGED])
def test_gate_level_job_round_trips_through_hdf5(tmp_path, meas_return):
    readouts = [
        CircuitReadout("bell", 2, (Measurement(12, 1), Measurement(13, 0))),
        CircuitReadout("x", 1, (Measurement(12, 0),)),
    ]
    datasets = [
        _get_dataset({0: [1j, 2j], 1: [3j, 4j]}),
        QDataset({"0": xr.DataArray(np.array([5j]), dims=("acq_index_0",))}),
    ]
    job = _get_job(
        MeasLvl.INTEGRATED,
        readouts=readouts,
        datasets=datasets,
        meas_return=meas_return,
    )
    file = tmp_path / "job.hdf5"

    save_job_in_hdf5(job, file)
    got = read_job_from_hdf5(file)

    assert got.readouts == readouts
    assert get_circuit_iq_memory(got) == get_circuit_iq_memory(job)


def _get_dataset(values_by_channel: Dict[int, List[complex]]) -> QDataset:
    """Gets the acquisitions of each channel, a row per shot"""
    return QDataset(
        {
            str(channel): xr.DataArray(
                np.asarray(values, dtype=complex).reshape(-1, 1),
                dims=("repetition", f"acq_index_{channel}"),
            )
            for channel, values in values_by_channel.items()
        }
    )


def _get_job(
    meas_level: MeasLvl,
    readouts: List[CircuitReadout],
    datasets: List[QDataset],
    meas_return: MeasRet = MeasRet.APPENDED,
) -> QuantumJob:
    """Gets a gate-level job of the readouts and their experiments' acquisitions"""
    return QuantumJob(
        tuid="20260101-000000-000-abcdef",
        meas_return=meas_return,
        meas_level=meas_level,
        meas_return_cols=1,
        n_qubits=3,
        job_id="job",
        readouts=readouts,
        raw_results={
            f"{readout.name}~{idx + 1}": dataset
            for idx, (readout, dataset) in enumerate(zip(readouts, datasets))
        },
    )


def _get_states(qubit_id: int, iq_points: np.ndarray) -> np.ndarray:
    """Discriminates each IQ point by its real part: 1 if positive"""
    return (np.real(iq_points) > 0).astype(int)
