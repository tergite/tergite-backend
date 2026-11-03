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

import numpy as np
import pytest

from libs.quantum_executor.base.quantum_job import (
    ByteOrder,
    _get_experiment_order,
    _get_register,
    _to_hex_shots,
)


def test_register_holds_each_bits_states_and_zero_elsewhere():
    register = _get_register({0: np.array([1, 0, 1]), 2: 1}, c_reg_len=3, shots=3)

    assert register.tolist() == [[1, 0, 1], [0, 0, 0], [1, 1, 1]]


@pytest.mark.parametrize(
    "byteorder, expected_hex",
    [(ByteOrder.LITTLE_ENDIAN, ["0x1", "0x4"]), (ByteOrder.BIG_ENDIAN, ["0x4", "0x1"])],
)
def test_each_shot_reads_as_hex(byteorder, expected_hex):
    register = np.array([[1, 0], [0, 0], [0, 1]], dtype=np.int8)

    assert _to_hex_shots(register, num_of_states=2, byteorder=byteorder) == expected_hex


def test_experiments_sort_by_index_then_unindexed_by_name():
    names = ["plain", "b~10", "c", "a~2"]

    assert sorted(names, key=_get_experiment_order) == ["a~2", "b~10", "c", "plain"]
