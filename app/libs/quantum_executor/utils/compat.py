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
"""Utility for compatibility handling between qiskit-dynamics and quantify"""
import uuid
from pathlib import Path

import settings

# Quantify
try:
    from quantify_core.data.handling import (
        create_exp_folder,
        gen_tuid,
        locate_experiment_container,
    )
    from quantify_core.data.types import TUID
    from quantify_scheduler import Schedule
    from quantify_scheduler.schedules import CompiledSchedule
except ImportError:
    from qiskit.pulse import Schedule

    CompiledSchedule = Schedule
    TUID = str

    def locate_experiment_container(
        tuid: TUID, datadir: Path | str | None = settings.EXECUTOR_DATA_DIR, **kwargs
    ):
        dir_path = Path(datadir) / str(tuid)
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path

    def create_exp_folder(
        tuid: TUID,
        name: str | None = None,
        datadir: Path | str | None = settings.EXECUTOR_DATA_DIR,
        **kwargs,
    ) -> str:
        folder_name = str(tuid)
        if name:
            folder_name += "-" + name

        folder_path = Path(datadir / folder_name)
        folder_path.mkdir(parents=True, exist_ok=True)

        return str(folder_path)

    def gen_tuid(**kwargs) -> TUID:
        return str(uuid.uuid4())
