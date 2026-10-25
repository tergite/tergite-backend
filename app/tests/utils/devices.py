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
from typing import List

import pytest


def load_test_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: List[str]
) -> List[str]:
    """Serves a device of each name, each with its own booking database, until the test ends

    Returns:
        the names of the devices loaded
    """
    import settings

    monkeypatch.setattr(settings, "_DEFAULT_DEVICE", settings._DEFAULT_DEVICE)
    monkeypatch.setattr(settings, "_DEVICES", settings._DEVICES)
    # the shell's variables would take precedence over every device's file
    monkeypatch.delenv("DEFAULT_PREFIX", raising=False)
    monkeypatch.delenv("BOOKING_DB_URL", raising=False)
    env_files = []
    for name in names:
        env_file = tmp_path / f"{name}.env"
        env_file.write_text(
            f"DEFAULT_PREFIX={name}\nBOOKING_DB_URL=sqlite:///{tmp_path}/{name}.db\n"
        )
        env_files.append(env_file)

    return settings.load_devices(env_files)
