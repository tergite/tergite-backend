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
"""Utilities for serving several devices in a test"""
from pathlib import Path
from typing import Dict, List, Optional

import pytest


def load_test_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    names: List[str],
    env_file_values: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Serves a device of each name, each with its own booking database and the given
    values in its env file, until the test ends

    Returns:
        the names of the devices loaded
    """
    import settings

    monkeypatch.setattr(settings, "_DEFAULT_DEVICE", settings._DEFAULT_DEVICE)
    monkeypatch.setattr(settings, "_DEVICES", settings._DEVICES)
    # the shell's variables would take precedence over every device's file
    monkeypatch.delenv("BCC_DEFAULT_PREFIX", raising=False)
    monkeypatch.delenv("BCC_BOOKING_DB_URL", raising=False)
    env_files = []
    for name in names:
        env_file = tmp_path / f"{name}.env"
        lines = [
            f"DEFAULT_PREFIX={name}",
            f"BOOKING_DB_URL=sqlite:///{tmp_path}/{name}.db",
        ]
        lines += [f"{key}={value}" for key, value in (env_file_values or {}).items()]
        env_file.write_text("\n".join(lines) + "\n")
        env_files.append(env_file)

    return settings.load_devices(env_files)
