# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Modified:
# - Stefan Hill, 2024
#
"""The settings of each device this process serves, read from its env file

One process serves several devices when load_devices is given several env files, the
first being the default one.
"""
import importlib.util
import os
from pathlib import Path
from typing import List, Optional

from utils.config import DeviceSettings, read_device_settings

# found, not imported, as importing it takes seconds and hundreds of MB
HAS_QISKIT_DYNAMICS = importlib.util.find_spec("qiskit_dynamics") is not None

_ROOT_PATH = Path(__file__).parent


def load_devices(env_files: List[Path]) -> List[str]:
    """Serves the devices of the given env files, the first being the default one

    Returns:
        the names of the devices, i.e. their default_prefix
    """
    global _DEVICES
    _DEVICES = _read_devices(env_files)
    return list(_DEVICES)


def get_device_names() -> List[str]:
    """Gets the names of the devices this process serves, the default one first"""
    return list(_DEVICES)


def get_device_settings(name: Optional[str] = None) -> DeviceSettings:
    """Gets the settings of the named device, or else of the default one"""
    return _DEVICES[name] if name else next(iter(_DEVICES.values()))


def _read_devices(env_files: List[Path]) -> dict[str, DeviceSettings]:
    """Reads the devices of the env files by name, in their order, the default first"""
    devices = [read_device_settings(Path(env_file).resolve()) for env_file in env_files]
    return {device.default_prefix: device for device in devices}


# BCC_ENV_FILE may name several, separated as in PATH, one per device
_DEVICES = _read_devices(
    [_ROOT_PATH / v for v in os.environ.get("BCC_ENV_FILE", ".env").split(os.pathsep)]
)
