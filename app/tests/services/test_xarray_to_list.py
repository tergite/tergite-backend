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
from typing import Dict

import numpy as np
import pytest
import xarray as xr

from ...libs.quantum_executor.base.quantum_job import xarray_to_list


def _make_iq_dataset(channels: Dict[int, np.ndarray]) -> xr.Dataset:
    """
    Build a Dataset whose data_vars are the per-channel IQ arrays supplied in `channels`.

    * If the supplied array is 2-D -> dims ("repetition", "acq")
    * If it is 1-D (averaged data) -> dims ("acq",)

    Returns
    -------
    xr.Dataset
    """
    data_vars = {}

    for idx, arr in channels.items():
        if arr.ndim == 2:  # (repetition, acq)
            dims = ("repetition", "acq")
        elif arr.ndim == 1:  # averaged, no repetition dim
            dims = ("acq",)
        else:
            # allow to build data var for tests to catch exception in xarray_to_list
            dims = tuple(f"dim{i}" for i in range(arr.ndim))

        data_vars[str(idx)] = xr.DataArray(arr, dims=dims, name=str(idx))

    return xr.Dataset(data_vars)


def _stub_job(raw_results):
    """Duck-typed minimal QuantumJob substitute – only .raw_results is accessed."""
    return SimpleNamespace(raw_results=raw_results)


def test_xarray_to_list_multi_rep_multi_channel():
    """
    This tests for measurement level 1 results.
    Two repetitions, two channels (0 & 2), three acquisition points each.
    """
    ch0 = np.array([[1 + 1j], [3 + 3j], [4 + 0j]])
    ch2 = np.array([[5 + 5j], [7 + 7j], [3 + 6j]])

    raw_results = {"exp": _make_iq_dataset({0: ch0, 2: ch2})}
    got = xarray_to_list(_stub_job(raw_results))

    expected = [
        [
            [(1.0, 1.0), (5.0, 5.0)],  # repetition 0
            [(3.0, 3.0), (7.0, 7.0)],  # repetition 1
            [(4.0, 0.0), (3.0, 6.0)],  # repetition 2
        ]
    ]
    assert got == expected


def test_xarray_to_list_averaged_data():
    ch0 = np.array([1 + 0j])
    ch1 = np.array([3 + 0j])

    raw_results = {"exp": _make_iq_dataset({0: ch0, 1: ch1})}
    got = xarray_to_list(_stub_job(raw_results))

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

    with pytest.raises(ValueError, match="unexpected ndarray shape"):
        xarray_to_list(_stub_job(raw_results))
