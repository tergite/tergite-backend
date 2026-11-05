# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""When a gate-level circuit's operations run: each as late as the operations after it on
what it holds let it, as qiskit's ALAP scheduling does, decided by a step of quantify's
device compilation, once the gates are pulses whose durations quantify knows

An operation holds transmons, and the classical bits it writes or reads, so that an
operation conditioned on a measurement would follow it."""

import math
from typing import Collection, Dict, List

from quantify_scheduler import Schedule
from quantify_scheduler.backends.graph_compilation import (
    SerialCompilationConfig,
    SimpleNodeConfig,
)
from quantify_scheduler.compilation import _determine_absolute_timing
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice
from quantify_scheduler.schedules.schedule import Schedulable, ScheduleBase

from .device import QBLOX_TIMEGRID_INTERVAL

# the key under which a circuit's schedule marks what each of its operations holds
HOLDS_KEY = "circuit_holds"
# quantify's step that times the schedule, which this one comes before
_TIMING_STEP = "determine_absolute_timing"


def get_compilation_config(device: QuantumDevice) -> SerialCompilationConfig:
    """Gets the device's compilation config, which times a circuit's operations as late as
    they can run before quantify times the rest

    Args:
        device: the quantum device

    Returns:
        the compilation config
    """
    config = device.generate_compilation_config()
    steps = config.device_compilation_config.compilation_passes
    timing = [step.name for step in steps].index(_TIMING_STEP)
    steps.insert(
        timing,
        SimpleNodeConfig(
            name="schedule_as_late_as_possible",
            compilation_func=schedule_as_late_as_possible,
        ),
    )
    return config


def get_clbit_name(clbit: int) -> str:
    """Gets the name a classical bit is held by, beside the transmons' names

    Args:
        clbit: the index of the classical bit

    Returns:
        the name, e.g. c[0]
    """
    return f"c[{clbit}]"


def mark_holds(schedulable: Schedulable, holds: Collection[str]) -> None:
    """Marks what an operation holds, its transmons and classical bits, which no operation
    before it that holds any of them may overlap, nor any after it

    Args:
        schedulable: the operation's schedulable
        holds: the names of the transmons and of the classical bits
    """
    schedulable.data[HOLDS_KEY] = sorted(holds)


def schedule_as_late_as_possible(
    schedule: Schedule, config: SerialCompilationConfig
) -> Schedule:
    """Times each marked operation, in the order they were added, to end as the next that
    holds any of what it holds starts, each transmon's and bit's last ending together,
    from the start of the first, which starts the schedule; a schedule of no marked
    operations is left as it is

    Args:
        schedule: the schedule, its gates compiled to pulses
        config: the compilation config

    Returns:
        the schedule, its marked operations timed
    """
    marked = [
        schedulable
        for schedulable in schedule.schedulables.values()
        if HOLDS_KEY in schedulable.data
    ]
    if not marked:
        return schedule
    durations = [_get_ticks(schedule, schedulable, config) for schedulable in marked]
    first, *others = marked
    first_tick, *ticks = _get_start_ticks(marked, durations)
    for schedulable, tick in zip(others, ticks):
        schedulable.data["timing_constraints"] = [
            {
                "ref_schedulable": first["name"],
                "ref_pt": "start",
                "ref_pt_new": "start",
                "rel_time": (tick - first_tick) * QBLOX_TIMEGRID_INTERVAL,
            }
        ]
    return schedule


def _get_ticks(
    schedule: Schedule, schedulable: Schedulable, config: SerialCompilationConfig
) -> int:
    """Gets the grid intervals the schedulable's operation takes, a part of one counting
    whole; a schedule within is timed first, as it is not until then"""
    operation = schedule.operations[schedulable["operation_id"]]
    if isinstance(operation, ScheduleBase):
        operation = _determine_absolute_timing(operation, config=config)
    return math.ceil(operation.duration / QBLOX_TIMEGRID_INTERVAL - 1e-9)


def _get_start_ticks(
    schedulables: List[Schedulable], durations: List[int]
) -> List[int]:
    """Gets the grid interval each schedulable starts at, each as late as those after it
    that hold any of what it holds let it, each held thing's last ending at 0"""
    # the tick each held thing is free until, counting back from the end at 0
    free_until: Dict[str, int] = {}
    starts = []
    for schedulable, duration in zip(reversed(schedulables), reversed(durations)):
        holds = schedulable.data[HOLDS_KEY]
        end = min((free_until.get(name, 0) for name in holds), default=0)
        start = end - duration
        for name in holds:
            free_until[name] = start
        starts.append(start)
    starts.reverse()
    return starts
