# This code is part of Tergite
#
# (C) Axel Andersson (2022)
# (C) Chalmers Next Labs (2025)
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
# Refactored by Stefan Hill (2024)
# Refactored by Chalmers Next Labs 2025

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Type

from quantify_scheduler import Schedule
from quantify_scheduler.operations.pulse_library import IdlePulse, ResetClockPhase
from quantify_scheduler.resources import ClockResource

from libs.qiskit.qobj import (
    PulseQobjConfig,
    PulseQobjExperiment,
    PulseQobjInstruction,
    QobjExperimentHeader,
)
from libs.quantum_executor.base.experiment import (
    NativeExperiment,
    copy_expt_header_with,
)

from ..base.quantum_job.dtos import NativeQobjConfig
from .channel import QuantifyChannelRegistry
from .instruction import (
    QBLOX_TIMEGRID_INTERVAL,
    AcquireInstruction,
    BaseInstruction,
    DelayInstruction,
    GaussPulseInstruction,
    InitialObjectInstruction,
    PulseLibInstruction,
    SetFreqInstruction,
    SetPhaseInstruction,
    ShiftFreqInstruction,
    ShiftPhaseInstruction,
    SquarePulseInstruction,
    WacqtCZPulseInstruction,
)

# Map (name, pulse_shape) => Quantify Instruction class
_INSTRUCTION_PULSE_MAP: Dict[Tuple[str, Optional[str]], Type[BaseInstruction]] = {
    ("setf", None): SetFreqInstruction,
    ("shiftf", None): ShiftFreqInstruction,
    ("setp", None): SetPhaseInstruction,
    ("fc", None): ShiftPhaseInstruction,
    ("delay", None): DelayInstruction,
    ("acquire", None): AcquireInstruction,
    ("parametric_pulse", "gaussian"): GaussPulseInstruction,
    ("parametric_pulse", "drag"): GaussPulseInstruction,
    ("parametric_pulse", "constant"): SquarePulseInstruction,
    ("parametric_pulse", "wacqt_cz"): WacqtCZPulseInstruction,
    ("parametric_pulse", "wacqt_cz_gate_pulse"): WacqtCZPulseInstruction,
}

FREQ_CONTROL_INSTRUCTIONS = (SetFreqInstruction, ShiftFreqInstruction)
PLACEHOLDER_CLOCK_FREQ_HZ = 0.0


@dataclass(frozen=True)
class QuantifyExperiment(NativeExperiment[Schedule]):
    """An experiment compiled to a quantify schedule, for the Qblox clusters"""

    channel_registry: QuantifyChannelRegistry

    @classmethod
    def from_qobj_expt(
        cls,
        expt: PulseQobjExperiment,
        name: str,
        qobj_config: PulseQobjConfig,
        native_config: NativeQobjConfig,
        hardware_map: Optional[Dict[str, Tuple[str, str]]],
        lo_frequencies: Optional[Dict[str, float]] = None,
        drive_frequencies: Optional[Dict[str, float]] = None,
        should_include_frequency_ops: bool = False,
    ) -> "QuantifyExperiment":
        """Converts PulseQobjExperiment to native experiment

        Args:
            expt: the pulse qobject experiment to translate
            name: the name of the experiment
            qobj_config: the pulse qobject config
            native_config: the native config for the qobj
            hardware_map: the map of the real/simulated device to the logical definitions

        Returns:
            the QiskitDynamicsExperiment corresponding to the PulseQobj
        """
        header = copy_expt_header_with(expt.header, name=name)
        channel_registry = QuantifyChannelRegistry()

        for inst in expt.instructions:
            _add_instruction_to_channel_registry(
                channel_registry=channel_registry,
                qobj_inst=inst,
                config=qobj_config,
                native_config=native_config,
                hardware_map=hardware_map,
                lo_frequencies=lo_frequencies,
                drive_frequencies=drive_frequencies,
            )

        schedule = _get_schedule(
            channel_registry=channel_registry,
            header=header,
            config=qobj_config,
            should_include_frequency_ops=should_include_frequency_ops,
        )
        duration = 0
        if isinstance(schedule.duration, (float, int)):
            duration = schedule.duration

        return cls(
            header=header,
            config=qobj_config,
            channel_registry=channel_registry,
            schedule=schedule,
            duration=duration,
        )


@dataclass(frozen=True)
class _ChannelPlan:
    clock: str
    schedulable: List[BaseInstruction]
    # whether the channel has no instructions, even ones left out of the schedule
    is_empty: bool


@dataclass
class _State:
    dispatch_tick: (
        int  # how many timing instructions have been "issued" on this channel
    )
    sync_tick: int  # accumulated global wait applied to this channel
    last_label: str  # last EMITTED op label (for tail idle)
    is_any_emitted: bool


def _add_instruction_to_channel_registry(
    channel_registry: QuantifyChannelRegistry,
    qobj_inst: PulseQobjInstruction,
    config: PulseQobjConfig,
    native_config: NativeQobjConfig,
    hardware_map: Optional[Dict[str, str]] = None,
    lo_frequencies: Optional[Dict[str, float]] = None,
    drive_frequencies: Optional[Dict[str, float]] = None,
):
    if hardware_map is None:
        hardware_map = {}

    key = (qobj_inst.name, getattr(qobj_inst, "pulse_shape", None))
    try:
        cls_instr = _INSTRUCTION_PULSE_MAP[key]
    except KeyError as exp:
        if qobj_inst.name in config.pulse_library:
            cls_instr = PulseLibInstruction  # fallback if defined in pulse library
        else:
            raise RuntimeError(
                f"No mapping for PulseQobjInstruction {qobj_inst}.\n{exp}"
            )
    instruction_kwargs = dict(
        config=config,
        native_config=native_config,
        channel_registry=channel_registry,
        hardware_map=hardware_map,
    )
    if cls_instr is ShiftPhaseInstruction:
        instruction_kwargs["lo_frequencies"] = lo_frequencies
        instruction_kwargs["drive_frequencies"] = drive_frequencies

    for instruction in cls_instr.list_from_qobj_inst(
        qobj_inst,
        **instruction_kwargs,
    ):
        channel_registry.add(instruction)


def _get_schedule(
    channel_registry: "QuantifyChannelRegistry",
    header: "QobjExperimentHeader",
    config: "PulseQobjConfig",
    timegrid_interval: float = QBLOX_TIMEGRID_INTERVAL,
    *,
    should_include_frequency_ops: bool = False,
) -> Schedule:
    """
    Build a Quantify Schedule.

    Ordering guarantee:
    - ClockResources are added before any schedule operations (schedule.add(...)).

    Frequency inference:
    - For each clock, use earliest `setf` by time (t0) as initial ClockResource frequency.
    - If `setf` has no t0, it is used only as a fallback.
    - Baseband clocks get 0 Hz.
    - If no setf exists, use placeholder.

    Feature flag:
    - should_include_frequency_ops=False keeps dynamic frequency commands in playback
      but excludes them from schedule operations.
    """
    schedule = Schedule(name=header.name, repetitions=config.shots)
    timegrid_interval = float(timegrid_interval)
    channel_plans = _get_channel_plans(channel_registry, should_include_frequency_ops)

    _add_clock_resources(schedule, _get_initial_clock_frequencies(channel_registry))
    drive_clocks = [
        channel.clock
        for channel in channel_registry.values()
        if _is_drive_clock(channel.clock)
    ]
    root_label = _add_root_and_phase_resets(schedule, config, drive_clocks)
    states = _add_aligned_operations(
        schedule, channel_plans, config, root_label, timegrid_interval
    )
    _add_tail_idles(schedule, channel_plans, states, root_label, timegrid_interval)
    return schedule


def _get_channel_plans(
    channel_registry: "QuantifyChannelRegistry", should_include_frequency_ops: bool
) -> List[_ChannelPlan]:
    """Gets each channel's instructions to schedule: all of them, or all but the ones
    that control the frequency"""
    return [
        _ChannelPlan(
            clock=channel.clock,
            schedulable=[
                inst
                for inst in channel.instructions
                if should_include_frequency_ops
                or not isinstance(inst, FREQ_CONTROL_INSTRUCTIONS)
            ],
            is_empty=not channel.instructions,
        )
        for channel in channel_registry.values()
    ]


def _get_initial_clock_frequencies(
    channel_registry: "QuantifyChannelRegistry",
) -> Dict[str, float]:
    """Gets each clock's frequency at the start: its earliest setf's, 0 Hz for a baseband
    clock, or the placeholder for a clock no setf sets"""
    frequencies = {}
    for channel in channel_registry.values():
        if _is_baseband_clock(channel.clock):
            frequencies[channel.clock] = 0.0
            continue
        set_freqs = [
            inst
            for inst in channel.instructions
            if isinstance(inst, SetFreqInstruction)
        ]
        earliest = min(set_freqs, key=lambda inst: inst.t0, default=None)
        frequencies[channel.clock] = (
            float(earliest.frequency)
            if earliest is not None
            else PLACEHOLDER_CLOCK_FREQ_HZ
        )
    return frequencies


def _add_clock_resources(schedule: Schedule, frequencies: Dict[str, float]) -> None:
    """Adds a clock resource for each clock, before any operation"""
    for clock, frequency in frequencies.items():
        schedule.add_resource(ClockResource(name=clock, freq=frequency))


def _add_root_and_phase_resets(
    schedule: Schedule, config: "PulseQobjConfig", drive_clocks: List[str]
) -> str:
    """Adds the idle every other operation starts after, and a phase reset at its start
    for each drive clock

    Returns:
        the idle's label
    """
    root_instruction = InitialObjectInstruction()
    schedule.add(
        ref_op=None,
        ref_pt="end",
        ref_pt_new="start",
        rel_time=0.0,
        label=root_instruction.label,
        operation=root_instruction.to_operation(config=config),
    )
    for clock in drive_clocks:
        schedule.add(
            ref_op=root_instruction.label,
            ref_pt="start",
            ref_pt_new="start",
            rel_time=0.0,
            label=f"reset_phase_{clock}",
            operation=ResetClockPhase(clock=clock),
        )
    return root_instruction.label


def _add_aligned_operations(
    schedule: Schedule,
    channel_plans: List[_ChannelPlan],
    config: "PulseQobjConfig",
    root_label: str,
    timegrid_interval: float,
) -> Dict[str, _State]:
    """Adds each instruction's operation at its time slot, aligned across the channels

    Each operation costs its channel one grid interval to dispatch, which shifts the
    channel's later operations, and so breaks the barriers that a common start time
    makes between channels. Each slot, in time order, raises every channel still active
    to the latest one's ready time, slot + sync + dispatch, by adding to its sync; each
    of its operations then starts at slot + sync + dispatch + 1, after the root, in grid
    ticks rather than floats, so that rounding does not accumulate.

    Returns:
        each clock's state after its last operation
    """
    by_clock_slot = _group_by_slot(channel_plans, timegrid_interval)
    last_slot_by_clock = {
        clock: max(slot_map, default=-1) for clock, slot_map in by_clock_slot.items()
    }
    sorted_slots = sorted(set().union(*by_clock_slot.values()))
    states = {
        plan.clock: _State(
            dispatch_tick=0, sync_tick=0, last_label=root_label, is_any_emitted=False
        )
        for plan in channel_plans
    }

    for slot in sorted_slots:
        active_clocks = [clock for clock in states if last_slot_by_clock[clock] >= slot]
        if not active_clocks:
            break
        _align_to_barrier(states, slot, active_clocks)

        for clock in active_clocks:
            state = states[clock]
            for inst in by_clock_slot[clock].get(slot, []):
                start_tick = slot + state.sync_tick + state.dispatch_tick + 1
                schedule.add(
                    ref_op=root_label,
                    ref_pt="end",
                    ref_pt_new="start",
                    rel_time=start_tick * timegrid_interval,
                    label=inst.label,
                    operation=inst.to_operation(config=config),
                )
                state.last_label = inst.label
                state.is_any_emitted = True
                state.dispatch_tick += 1
    return states


def _group_by_slot(
    channel_plans: List[_ChannelPlan], timegrid_interval: float
) -> Dict[str, Dict[int, List[BaseInstruction]]]:
    """Groups each clock's instructions by the grid tick they start at"""
    by_clock_slot = {}
    for plan in channel_plans:
        slot_map: Dict[int, List[BaseInstruction]] = defaultdict(list)
        for inst in plan.schedulable:
            slot_map[_to_tick(inst.t0, timegrid_interval)].append(inst)
        by_clock_slot[plan.clock] = slot_map
    return by_clock_slot


def _align_to_barrier(
    states: Dict[str, _State], slot: int, active_clocks: List[str]
) -> None:
    """Raises the active clocks' syncs so that each is ready at the slot when the
    latest is"""
    barrier = max(
        slot + states[clock].sync_tick + states[clock].dispatch_tick
        for clock in active_clocks
    )
    for clock in active_clocks:
        state = states[clock]
        state.sync_tick += barrier - (slot + state.sync_tick + state.dispatch_tick)


def _add_tail_idles(
    schedule: Schedule,
    channel_plans: List[_ChannelPlan],
    states: Dict[str, _State],
    root_label: str,
    timegrid_interval: float,
) -> None:
    """Adds an idle after the last operation of each channel that had instructions,
    even if none was scheduled"""
    for plan in channel_plans:
        if plan.is_empty:
            continue
        state = states[plan.clock]
        ref = state.last_label if state.is_any_emitted else root_label
        schedule.add(
            ref_op=ref,
            ref_pt="end",
            ref_pt_new="start",
            rel_time=timegrid_interval,
            label=f"{ref}__tail_idle",
            operation=IdlePulse(duration=timegrid_interval),
        )


def _to_tick(t: float, timegrid_interval: float) -> int:
    """Gets the grid tick nearest the time in seconds"""
    return int(math.floor(t / timegrid_interval + 0.5 + 1e-12))


def _is_drive_clock(clock: str) -> bool:
    # qXX.01 or qXX.12
    parts = clock.split(".")
    return len(parts) == 2 and parts[1] in {"01", "12"}


def _is_baseband_clock(clock: str) -> bool:
    # baseband clock can be 0
    return clock.endswith(".baseband") or clock == "cl0.baseband"
