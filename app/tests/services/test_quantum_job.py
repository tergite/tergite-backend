# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from types import SimpleNamespace
from typing import Dict, List

import numpy as np
import pytest
import xarray as xr

from ...libs.quantum_executor.base.quantum_job import (
    ByteOrder,
    discriminate_results,
)
from ...libs.quantum_executor.base.quantum_job.dtos import MeasLvl, MeasRet, QuantumJob
from ...libs.quantum_executor.base.quantum_job.typing import QDataset


def _make_qdataset(channels: Dict[int, np.ndarray]) -> QDataset:
    """
    Build a QDataset with one or more acquisition channels.
    `channels` maps channel-index → 1-D array of discriminator bits.
    """
    data_vars = {}
    for idx, bits in channels.items():
        da = xr.DataArray(
            bits.reshape(-1, 1), dims=("repetition", "acq"), name=str(idx)
        )
        data_vars[str(idx)] = da
    return QDataset(data_vars)


def _dummy_qobj(mem_slots: int, channels: List[int]):
    """
    Build a `minimal stub` that looks like a PulseQobj for the parts
    used by `get_acquisition_parameters_from_experiment`.

    One experiment, one `acquire` instruction that lists all measured
    qubits and memory slots 1-to-1.
    """
    # fake instruction object
    acquire_inst = SimpleNamespace(
        name="acquire",
        qubits=channels,
        memory_slot=channels,
        t0=0,
        duration=0,
    )
    # fake experiment object
    experiment = SimpleNamespace(instructions=[acquire_inst])
    # fake qobj object
    return SimpleNamespace(experiments=[experiment])


@pytest.mark.parametrize(
    "mem_slots, channels, byteorder, expected_hex",
    [
        # 2-qubit register – measure qubit 1 only
        (2, {1: np.array([0, 1, 1])}, ByteOrder.LITTLE_ENDIAN, ["0x0", "0x2", "0x2"]),
        # 2-qubit register – measure qubit 0 only
        (
            2,
            {0: np.array([1, 0, 1, 1])},
            ByteOrder.LITTLE_ENDIAN,
            ["0x1", "0x0", "0x1", "0x1"],
        ),
        # 2-qubit, big-endian
        (2, {1: np.array([0, 1, 0])}, ByteOrder.BIG_ENDIAN, ["0x0", "0x1", "0x0"]),
        # 4-qubit register – measure qubits 1 & 3
        (
            4,
            {1: np.array([0, 1]), 3: np.array([1, 0])},
            ByteOrder.LITTLE_ENDIAN,
            ["0x8", "0x2"],
        ),
        # 4-qubit register – measure qubit 0 only
        (4, {0: np.array([1, 1, 0])}, ByteOrder.LITTLE_ENDIAN, ["0x1", "0x1", "0x0"]),
    ],
)
def test_discriminate_various_subsets(mem_slots, channels, byteorder, expected_hex):
    # build raw xarray dataset with one experiment called "exp"
    ds = _make_qdataset(channels)
    raw_results = {"exp": ds}

    # minimal dummy PulseQobj with matching acquire information
    qobj = _dummy_qobj(mem_slots, sorted(channels.keys()))

    # assemble the QuantumJob
    job = QuantumJob(
        tuid="t",
        meas_return=MeasRet.APPENDED,
        meas_level=MeasLvl.DISCRIMINATED,
        meas_return_cols=ds[list(ds.data_vars)[0]].shape[0],
        n_qubits=mem_slots,
        memory_slot_size=mem_slots,
        raw_results=raw_results,
        qobj=qobj,
    )

    # trivial discriminator: just echoes the bits back
    def disc(idx, iq):
        return iq.astype(np.int8)

    got = discriminate_results(job, disc, byteorder=byteorder)
    assert got == [expected_hex]
