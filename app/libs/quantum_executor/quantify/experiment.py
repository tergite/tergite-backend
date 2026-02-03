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
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Set, Tuple, Type, List

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


@dataclass(frozen=True)
class _ClockInitCandidate:
    t0: float
    freq_hz: float


@dataclass(frozen=True)
class _ChannelPlan:
    schedulable: List[BaseInstruction]
    had_any_instructions: bool


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
            channel_registry=channel_registry, header=header, config=qobj_config
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

    # Single pass over channel_registry: collect what we need
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
    schedule.add(
        ref_op=None,
        ref_pt="end",
        ref_pt_new="start",
        rel_time=0.0,
        label=root_instruction.label,
        operation=root_instruction.to_operation(config=config),
    )

    # Phase resets at the very start (after resources exist).
    for clock in drive_clocks:
        schedule.add(
            ref_op=root.label,
            ref_pt="start",
            ref_pt_new="start",
            rel_time=0.0,
            label=f"reset_phase_{clock}",
            operation=ResetClockPhase(clock=clock),
        )

    tg = float(timegrid_interval)

    for plan in channel_plans:
        prev: BaseInstruction = root_instruction

        for curr in plan.schedulable:
            # These MUST exist for schedulable instructions
            rel_time = curr.t0 - prev.final_timestamp + tg

            schedule.add(
                ref_op=prev.label,
                ref_pt="end",
                ref_pt_new="start",
                rel_time=rel_time,
                label=curr.label,
                operation=curr.to_operation(config=config),
            )
            prev = curr

        # tail idle if the channel had ANY instructions,
        # even if all were filtered out as freq-control ops.
        if plan.had_any_instructions:
            schedule.add(
                ref_op=prev.label,
                ref_pt="end",
                ref_pt_new="start",
                rel_time=tg,
                label=f"{prev.label}__tail_idle",
                operation=IdlePulse(duration=tg),
            )

    return schedule
