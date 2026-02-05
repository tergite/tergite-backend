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
from typing import Dict, List, Optional

import numpy as np
import pytest
import xarray as xr

from ...libs.quantum_executor.base.quantum_job import xarray_to_list


def _make_iq_dataset(slots: Dict[int, np.ndarray]) -> xr.Dataset:
    """
    Build a Dataset whose data_vars are per-memory-slot IQ arrays.

    Keys MUST be memory slots (strings), e.g. "0", "1", ...
    """
    data_vars = {}

    for slot, arr in slots.items():
        if arr.ndim == 2:  # (repetition, acq)
            dims = ("repetition", "acq")
        elif arr.ndim == 1:  # averaged, no repetition dim
            dims = ("acq",)
        else:
            dims = tuple(f"dim{i}" for i in range(arr.ndim))

        data_vars[str(slot)] = xr.DataArray(arr, dims=dims, name=str(slot))

    return xr.Dataset(data_vars)

def _dummy_qobj(*, qubits: List[int], slots: Optional[List[int]] = None):
    """
    Minimal qobj stub for xarray_to_list:
    - qobj.experiments[0].instructions includes acquire with qubits + memory_slot
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
    experiment = SimpleNamespace(instructions=[acquire_inst])
    return SimpleNamespace(experiments=[experiment])

def _stub_job(raw_results, qobj=None):
    if qobj is None:
        qobj = _dummy_qobj(qubits=[0], slots=[0])
    return SimpleNamespace(raw_results=raw_results, qobj=qobj)


def test_xarray_to_list_multi_rep_multi_channel():
    """
    Two *measured qubits*, compacted into slots 0 and 1:
      qubit 0 -> slot 0
      qubit 2 -> slot 1
    """
    # 3 repetitions, 1 acquisition per repetition
    ch0 = np.array([[1 + 1j], [3 + 3j], [4 + 0j]])  # qubit 0
    ch2 = np.array([[5 + 5j], [7 + 7j], [3 + 6j]])  # qubit 2

    # IMPORTANT: dataset keys are slots, not qubit indices
    raw_results = {"exp": _make_iq_dataset({0: ch0, 1: ch2})}
    qobj = _dummy_qobj(qubits=[0, 2], slots=[0, 1])

    got = xarray_to_list(_stub_job(raw_results, qobj=qobj))

    expected = [
        [
            [(1.0, 1.0), (5.0, 5.0)],  # repetition 0
            [(3.0, 3.0), (7.0, 7.0)],  # repetition 1
            [(4.0, 0.0), (3.0, 6.0)],  # repetition 2
        ]
    ]
    assert got == expected


def test_xarray_to_list_averaged_data():
    slot0 = np.array([1 + 0j])
    slot1 = np.array([3 + 0j])

    raw_results = {"exp": _make_iq_dataset({0: slot0, 1: slot1})}
    qobj = _dummy_qobj(qubits=[0, 1], slots=[0, 1])

    got = xarray_to_list(_stub_job(raw_results, qobj=qobj))

    expected = [[[(1.0, 0.0), (3.0, 0.0)]]]
    assert got == expected


def test_xarray_to_list_none_raw_results():
    with pytest.raises(
        ValueError,
        match="xarray_to_list: Can't find Quantum job experiment results `job.raw_results`.",
    ):
        xarray_to_list(_stub_job(raw_results=None))


def test_xarray_to_list_non_dataset_value():
    raw_results = {"exp": "not a dataset"}
    with pytest.raises(TypeError, match="xarray.Dataset"):
        xarray_to_list(_stub_job(raw_results))


def test_xarray_to_list_unexpected_ndim():
    # more than 2 dimensions, so should raise ValueError
    bad_arr = np.zeros((1, 1, 1), dtype=np.complex128)
    raw_results = {"exp": _make_iq_dataset({0: bad_arr})}
    qobj = _dummy_qobj(qubits=[0], slots=[0])

    with pytest.raises(ValueError, match="unexpected ndarray shape"):
        xarray_to_list(_stub_job(raw_results))

def test_xarray_to_list_orders_by_memory_slot_not_qubit_order():
    """
    Ensure ordering is by increasing memory_slot, even if acquire lists qubits/slots out of order.
    Here: qubit 7 -> slot 3, qubit 2 -> slot 0.
    Output must be [slot 0, slot 3].
    """
    slot0 = np.array([[10 + 1j], [11 + 2j]])  # 2 reps
    slot3 = np.array([[20 + 3j], [21 + 4j]])

    raw_results = {"exp": _make_iq_dataset({3: slot3, 0: slot0})}
    qobj = _dummy_qobj(qubits=[7, 2], slots=[3, 0])  # intentionally unsorted

    got = xarray_to_list(_stub_job(raw_results, qobj=qobj))

    expected = [
        [
            [(10.0, 1.0), (20.0, 3.0)],  # rep0: slot0 then slot3
            [(11.0, 2.0), (21.0, 4.0)],  # rep1
        ]
    ]
    assert got == expected