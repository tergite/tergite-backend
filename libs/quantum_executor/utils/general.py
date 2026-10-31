# This code is part of Tergite
#
# (C) Axel Andersson (2022)
# (C) Martin Ahindura (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Refactored by Martin Ahindura (2024)

import json
import sys
from typing import TypeVar, Union

import h5py

_T = TypeVar("_T")


def search_nested(haystack: Union[h5py.Group, dict], needle: str):
    """Searches a nested dict-like object for a given key

    Args:
        haystack: the nested dict-like object to search in
        needle: the key to search for

    Returns:
        an iterator of the path segments from root to the given key e.g. ("foo", "bar", needle)
    """
    if not isinstance(haystack, h5py.Group) and not isinstance(haystack, dict):
        return

    if needle in haystack:
        yield (needle,)

    for key, val in haystack.items():
        for sub_path in search_nested(val, needle):
            yield key, *sub_path


def load_config(filepath: str):
    with open(filepath, mode="r") as _j:
        try:
            return json.load(_j)
        except json.JSONDecodeError as jsde:
            print(
                f"Could not read configuration .json file {_j.name}. Incorrectly formatted?",
                file=sys.stderr,
            )
            raise jsde
