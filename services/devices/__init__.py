# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""The device: its properties, calibration and switch"""

from .service import (
    get_device_calibration_info,
    get_device_info,
    is_offline,
    save_all_device_params,
    save_device_calibration,
    switch_off,
    switch_on,
)

__all__ = [
    "get_device_calibration_info",
    "get_device_info",
    "is_offline",
    "save_all_device_params",
    "save_device_calibration",
    "switch_off",
    "switch_on",
]
