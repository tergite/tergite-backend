# This code is part of Tergite
#
# (C) Copyright Abdullah-Al Amin 2023
# (C) Copyright Martin Ahindura 2024
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from typing import Dict, List, Literal, Union


def attach_units_many(
    data: List[Dict[str, Union[str, float]]], units_map: Dict[str, str]
) -> List[Dict[str, Dict[Literal["value", "unit"], Union[str, float]]]]:
    """Attaches units to the values in a list of dicts

    Args:
        data: the records to be transformed
        units_map: the map of property name to its unit

    Returns:
        the list of records with values of form {"value": ..., "unit": ...}
    """
    return [attach_units(item, units_map) for item in data]


def attach_units(
    data: Dict[str, Union[str, float]], units_map: Dict[str, str]
) -> Dict[str, Dict[Literal["value", "unit"], Union[str, float]]]:
    """Attaches units to the values in the dict

    Args:
        data: the record to be transformed
        units_map: the map of property name to its unit

    Returns:
        the record with values of form {"value": ..., "unit": ...}
    """
    return {k: {"value": v, "unit": units_map.get(k, "")} for k, v in data.items()}
