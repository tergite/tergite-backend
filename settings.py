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

`settings.X` is X of the current device: the one using_device names for the request or
job at hand, or else the default device, the first loaded. One process serves several
devices when load_devices is given several env files.
"""
import importlib.util
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, List, Optional

from app.utils.config import DeviceSettings, read_device_settings

# found, not imported, as importing it takes seconds and hundreds of MB
HAS_QISKIT_DYNAMICS = importlib.util.find_spec("qiskit_dynamics") is not None

_ROOT_PATH = Path(__file__).parent


# FIXME: This __getattr__ indirection will confuse readers of the code base. Better if explicitly got.
def __getattr__(name: str):
    # settings.X is the current device's x, until step 4 hands each its settings
    return getattr(get_device_settings(), name.lower())


def load_devices(env_files: List[Path]) -> List[str]:
    """Serves the devices of the given env files, the first being the default one

    Returns:
        the names of the devices, i.e. their default_prefix
    """
    global _DEFAULT_DEVICE, _DEVICES
    _DEFAULT_DEVICE, _DEVICES = _read_devices(env_files)
    return list(_DEVICES)


def get_device_names() -> List[str]:
    """Gets the names of the devices this process serves, the default one first"""
    return list(_DEVICES)


def get_device_settings(name: Optional[str] = None) -> DeviceSettings:
    """Gets the settings of the named device, or else of the current one"""
    name = name or _CURRENT_DEVICE.get()
    return _DEVICES[name] if name else _DEFAULT_DEVICE


@contextmanager
def using_device(name: str) -> Iterator[DeviceSettings]:
    """Makes the named device the current one within the block"""
    token = _CURRENT_DEVICE.set(name)
    try:
        yield _DEVICES[name]
    finally:
        _CURRENT_DEVICE.reset(token)


def _read_devices(
    env_files: List[Path],
) -> tuple[DeviceSettings, dict[str, DeviceSettings]]:
    """Reads the devices of the env files: the first, the default one, and all by name"""
    devices = [read_device_settings(Path(env_file).resolve()) for env_file in env_files]
    return devices[0], {device.default_prefix: device for device in devices}


# BCC_ENV_FILE may name several, separated as in PATH, one per device
_DEFAULT_DEVICE, _DEVICES = _read_devices(
    [_ROOT_PATH / v for v in os.environ.get("BCC_ENV_FILE", ".env").split(os.pathsep)]
)
_CURRENT_DEVICE: ContextVar[Optional[str]] = ContextVar("current_device", default=None)
