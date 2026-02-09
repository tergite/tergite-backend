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


from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple, Type

from qiskit.qobj import (
    PulseQobjConfig,
    PulseQobjExperiment,
    PulseQobjInstruction,
    QobjExperimentHeader,
)
from quantify_scheduler import Schedule
from quantify_scheduler.operations.pulse_library import IdlePulse, ResetClockPhase
from quantify_scheduler.resources import ClockResource

from app.libs.quantum_executor.base.experiment import (
    NativeExperiment,
    copy_expt_header_with,
)
from app.libs.quantum_executor.quantify.channel import QuantifyChannel

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
    ("parametric_pulse", "constant"): SquarePulseInstruction,
    ("parametric_pulse", "wacqt_cz"): WacqtCZPulseInstruction,
    ("parametric_pulse", "wacqt_cz_gate_pulse"): WacqtCZPulseInstruction,
}

FREQ_CONTROL_INSTRUCTIONS = (SetFreqInstruction, ShiftFreqInstruction)
PLACEHOLDER_CLOCK_FREQ_HZ = 0.0


def _is_drive_clock(clock: str) -> bool:
    # qXX.01 or qXX.12
    parts = clock.split(".")
    return len(parts) == 2 and parts[1] in {"01", "12"}


def _is_baseband_clock(clock: str) -> bool:
    # baseband clock can be 0
    return clock.endswith(".baseband") or clock == "cl0.baseband"


@dataclass(frozen=True)
class _ClockInitCandidate:
    t0: float
    freq_hz: float


@dataclass(frozen=True)
class _ChannelPlan:
    clock: str
    schedulable: List[BaseInstruction]
    had_any_instructions: bool


@dataclass
class _State:
    prev_label: str
    prev_final_tick: int  # t0 + duration
    cursor_end_tick: int  # schedule-time of last emitted op
    pending_sync_tick: int  # accumulated global waiting to apply at next op


@dataclass(frozen=True)
class QuantifyExperiment(NativeExperiment[Schedule]):
    channel_registry: QuantifyChannelRegistry

    @classmethod
    def from_qobj_expt(
        cls,
        expt: PulseQobjExperiment,
        name: str,
        qobj_config: PulseQobjConfig,
        native_config: NativeQobjConfig,
        hardware_map: Optional[Dict[str, Tuple[str, str]]],
        include_dynamic_frequency_ops: bool = False,
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
            )

        schedule = _construct_schedule(
            channel_registry=channel_registry,
            header=header,
            config=qobj_config,
            include_dynamic_frequency_ops=include_dynamic_frequency_ops,
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


def _add_instruction_to_channel_registry(
    channel_registry: QuantifyChannelRegistry,
    qobj_inst: PulseQobjInstruction,
    config: PulseQobjConfig,
    native_config: NativeQobjConfig,
    hardware_map: Optional[Dict[str, str]] = None,
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
    for instruction in cls_instr.list_from_qobj_inst(
        qobj_inst,
        config=config,
        native_config=native_config,
        channel_registry=channel_registry,
        hardware_map=hardware_map,
    ):
        if instruction.name != "ResetClockPhase":
            instruction.register()


def _construct_schedule(
    channel_registry: "QuantifyChannelRegistry",
    header: "QobjExperimentHeader",
    config: "PulseQobjConfig",
    timegrid_interval: float = QBLOX_TIMEGRID_INTERVAL,
    *,
    include_dynamic_frequency_ops: bool = False,
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
    - include_dynamic_frequency_ops=False keeps dynamic frequency commands in playback
      but excludes them from schedule operations.
    """

    schedule = Schedule(name=header.name, repetitions=config.shots)

    # Single pass over channel_registry: collect channel_plans and initial clock frequency for each channel
    drive_clocks: Set[str] = set()
    init_freq_by_clock: Dict[str, Optional[_ClockInitCandidate]] = {}
    channel_plans: List[_ChannelPlan] = []

    # get clock frequencies and list of instructions
    for channel in channel_registry.values():  # QuantifyChannel
        clock = channel.clock

        if _is_drive_clock(clock):
            drive_clocks.add(clock)

        instructions = channel.instructions
        had_any = bool(instructions)

        # Keep best init candidate per clock
        best: Optional[_ClockInitCandidate] = init_freq_by_clock.get(clock)
        schedulable: List[BaseInstruction] = []

        for inst in instructions:
            # Infer init frequency from SetFreqInstruction
            if isinstance(inst, SetFreqInstruction):
                cand = _ClockInitCandidate(t0=inst.t0, freq_hz=float(inst.frequency))
                if best is None or cand.t0 < best.t0:
                    best = cand

            if (not include_dynamic_frequency_ops) and isinstance(
                inst, FREQ_CONTROL_INSTRUCTIONS
            ):
                continue

            schedulable.append(inst)

        init_freq_by_clock[clock] = best

        channel_plans.append(
            _ChannelPlan(
                clock=clock, schedulable=schedulable, had_any_instructions=had_any
            )
        )

    # add clock resources first
    for clock, cand in init_freq_by_clock.items():
        if _is_baseband_clock(clock):
            freq_hz = 0.0
        else:
            freq_hz = cand.freq_hz if cand is not None else PLACEHOLDER_CLOCK_FREQ_HZ

        schedule.add_resource(ClockResource(name=clock, freq=freq_hz))

    # add schedule operations
    root_instruction = InitialObjectInstruction()
    root_op = root_instruction.to_operation(config=config)
    schedule.add(
        ref_op=None,
        ref_pt="end",
        ref_pt_new="start",
        rel_time=0.0,
        label=root_instruction.label,
        operation=root_op,
    )

    # Phase resets at the very start (after resources exist).
    for clock in drive_clocks:
        schedule.add(
            ref_op=root_instruction.label,
            ref_pt="start",
            ref_pt_new="start",
            rel_time=0.0,
            label=f"reset_phase_{clock}",
            operation=ResetClockPhase(clock=clock),
        )

    timegrid_interval = float(timegrid_interval)

    """
    tl;dr: We keep track timegrid shifts and synchronize it accross the channels

    As we add timegrid_interval to our original scheduled operations.
    We shift the start time of the next operation, 
        therefore breaking alignment/barriers accross the channels
    We need to compensate for that by keeping track of a "global clock".
    
    A few things to consider like including dynamic frequency ops in schedule plan 
        and float operations accumulating small errors.

    Plan: 

    grid_ticks function converts float to int in nanoseconds

    Convert channel plan to group by time slot: 
        by_clock[clock][slot_tick]  
    Build a gloal slot list:
        slots = sorted(union of all slot_ticks accross all clocks)
    
    maintain per clock state: 
        prev_label - last scheduled op 
        prev_final_tick - original planned (Qobj) time of last scheduled op 
        cursor_end_tick - actual schedule time when channel is free next 
        pending_sync_tick - extra global delay accumulated while channel was idle  

    Per slot S:
        1. Compute barrier_ready_tick 
            - ready_tick(clock) = cursor_end_tick + pending_sync_tick 
            - barrier = max(ready_tick(clock) for clocks that still have future work)
        2. For each clock:
            - slack = barrier - ready_tick(clock)
            - if clock has instruction at slot S:
                * apply (pending_sync + slack) once to  the first instruction rel_time 
                * clear pending_sync_tick = 0
                * update cursor_end_tick, prev_final_tick, prev_label 
            - if no instructons at this slot:
                *  accumulate pending_sync_tick += slack
    """

    def to_tick(t: float) -> int:
        return int(round(t / timegrid_interval))

    def from_tick(k: int) -> float:
        return k * timegrid_interval

    root_end_tick = to_tick(float(getattr(root_op, "duration", 0.0) or 0.0))

    # Group instructions by (clock, slot_tick)
    by_clock_slot: Dict[str, Dict[int, List[BaseInstruction]]] = {}
    slots: Set[int] = set()
    clocks_in_order: List[str] = []
    slot_map: Dict[int, List[BaseInstruction]] = defaultdict(list)

    for plan in channel_plans:
        clocks_in_order.append(plan.clock)
        slot_map = defaultdict(list)
        for inst in plan.schedulable:
            s = to_tick(inst.t0)
            slot_map[s].append(inst)
            slots.add(s)
        by_clock_slot[plan.clock] = slot_map

    sorted_slots = sorted(slots)

    has_any_sched: Dict[str, bool] = {
        plan.clock: bool(plan.schedulable) for plan in channel_plans
    }

    state: Dict[str, _State] = {
        clock: _State(
            prev_label=root_instruction.label,
            prev_final_tick=0,
            cursor_end_tick=root_end_tick,
            pending_sync_tick=0,
        )
        for clock in clocks_in_order
    }

    # Iterate global slots in increasing Qobj time
    for slot in sorted_slots:

        active_clocks = clocks_in_order

        # barrier: align all active channels
        barrier_tick = max(
            state[c].cursor_end_tick + state[c].pending_sync_tick for c in active_clocks
        )

        for clock in active_clocks:
            st = state[clock]
            ready = st.cursor_end_tick + st.pending_sync_tick
            slack = barrier_tick - ready
            if slack < 0:
                slack = 0

            insts = by_clock_slot[clock].get(slot)
            if not insts:
                # no op, carry the required waiting forward
                st.pending_sync_tick += slack
                continue

            # Has ops at this slot: apply (pending + slack) once, then clear pending
            extra_sync = st.pending_sync_tick + slack
            st.pending_sync_tick = 0

            first = True
            for inst in insts:
                curr_t0_tick = slot
                dur_tick = to_tick(inst.duration)
                prev_final = st.prev_final_tick

                base_rel_tick = (curr_t0_tick - prev_final) + 1
                rel_tick = base_rel_tick + (extra_sync if first else 0)
                first = False

                if rel_tick < 0:
                    raise ValueError(
                        f"Negative rel_time on clock={clock}. "
                        f"slot={slot} prev_final={prev_final} rel_tick={rel_tick} "
                        f"(inst={inst})"
                    )

                schedule.add(
                    ref_op=st.prev_label,
                    ref_pt="end",
                    ref_pt_new="start",
                    rel_time=from_tick(rel_tick),
                    label=inst.label,
                    operation=inst.to_operation(config=config),
                )

                # advance per-channel schedule-time cursor
                st.cursor_end_tick = st.cursor_end_tick + rel_tick + dur_tick
                st.prev_final_tick = curr_t0_tick + dur_tick
                st.prev_label = inst.label

    for plan in channel_plans:
        # tail idle if the channel had ANY instructions,
        # even if all were filtered out as freq-control ops.
        if plan.had_any_instructions:
            st = state[plan.clock]
            schedule.add(
                ref_op=st.prev_label,
                ref_pt="end",
                ref_pt_new="start",
                rel_time=timegrid_interval,
                label=f"{st.prev_label}__tail_idle",
                operation=IdlePulse(duration=timegrid_interval),
            )
    return schedule
