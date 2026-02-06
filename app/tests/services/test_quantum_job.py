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


def _dummy_qobj(mem_slots: int, qubits: List[int], slots: List[int] | None = None):
    """
    Mimic the Qobj shape after SDK patch:
    - qobj.config.memory_slots exists
    - experiment.header.memory_slots exists
    - acquire.qubits and acquire.memory_slot exist (and may differ)
    """
    if slots is None:
        slots = list(qubits)

    acquire_inst = SimpleNamespace(
        name="acquire",
        qubits=list(qubits),
        memory_slot=list(slots),
        t0=0,
        duration=0,
    )

    experiment = SimpleNamespace(
        instructions=[acquire_inst],
        header=SimpleNamespace(memory_slots=mem_slots, metadata={}),
    )

    return SimpleNamespace(
        experiments=[experiment],
        config=SimpleNamespace(memory_slots=mem_slots),
    )


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


def test_discriminate_compacted_mapping():
    # slot 0 contains bits for qubit 1, slot 1 contains bits for qubit 3
    ds = _make_qdataset({0: np.array([0, 1]), 1: np.array([1, 0])})
    raw_results = {"exp": ds}

    qobj = _dummy_qobj(mem_slots=4, qubits=[1, 3], slots=[0, 1])

    job = QuantumJob(
        tuid="t",
        meas_return=MeasRet.APPENDED,
        meas_level=MeasLvl.DISCRIMINATED,
        meas_return_cols=ds[list(ds.data_vars)[0]].shape[0],
        n_qubits=4,
        memory_slot_size=4,
        raw_results=raw_results,
        qobj=qobj,
    )

    def disc(idx, iq):
        return iq.astype(np.int8)

    got = discriminate_results(job, disc, byteorder=ByteOrder.LITTLE_ENDIAN)

    # Register length 4, bits at slots 0 and 1 only:
    # rep0: slot0=0, slot1=1 => 0b0010 => 0x2
    # rep1: slot0=1, slot1=0 => 0b0001 => 0x1
    assert got == [["0x2", "0x1"]]
