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
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Type

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


from quantify_scheduler.operations.pulse_library import 
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

_FREQ_CONTROL_NAMES = {"setf", "shiftf"}



_READOUT_GUARD_TIME = 20e-9  # 40 ns
PLACEHOLDER_CLOCK_FREQ_HZ = 0.0  # safe placeholder when nothing runs on the channel



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
    channel_registry: QuantifyChannelRegistry,
    header: QobjExperimentHeader,
    config: PulseQobjConfig,
    timegrid_interval: float = QBLOX_TIMEGRID_INTERVAL,
) -> Schedule:
    raw_schedule = Schedule(name=header.name, repetitions=config.shots)
    _add_phase_resets_first(raw_schedule, channel_registry)
    # clocks first
    _add_clock_resources_first(schedule=raw_schedule, channel_registry=channel_registry)
    # enforce MW -> RO separation across channels
    _enforce_drive_to_readout_guard_time(
        channel_registry=channel_registry,
        guard_time=40e-9,
        grid=timegrid_interval,
    )

    root_instruction = InitialObjectInstruction()
    raw_schedule.add(
        ref_op=None,
        ref_pt="end",
        ref_pt_new="start",
        rel_time=0.0,
        label=root_instruction.label,
        operation=root_instruction.to_operation(config=config),
    )

    for channel in channel_registry.values():  # type: QuantifyChannel
        prev = root_instruction

        for curr in channel.instructions:
            # DO NOT schedule dynamic frequency commands as operations
            if getattr(curr, "name", None) in _FREQ_CONTROL_NAMES:
                # keep them in playback (already registered), but skip adding an op
                continue

            rel_time = curr.t0 - prev.final_timestamp + timegrid_interval
            raw_schedule.add(
                ref_op=prev.label,
                ref_pt="end",
                ref_pt_new="start",
                rel_time=rel_time,
                label=curr.label,
                operation=curr.to_operation(config=config),
            )
            prev = curr

        if len(channel.instructions) > 0:
            raw_schedule.add(
                ref_op=prev.label,
                ref_pt="end",
                ref_pt_new="start",
                rel_time=timegrid_interval,
                label=f"{prev.label}__tail_idle",
                operation=IdlePulse(duration=timegrid_interval),
            )

    return raw_schedule



def _infer_initial_clock_frequency(channel: QuantifyChannel) -> float:
    """
    Infer initial clock frequency from earliest SetFreqInstruction ('setf') on that channel.
    If none exists, return 0.0.
    """
    earliest_setf = None
    for inst in channel.instructions:
        if getattr(inst, "name", None) == "setf" and hasattr(inst, "frequency"):
            if earliest_setf is None or inst.t0 < earliest_setf.t0:
                earliest_setf = inst
    return float(earliest_setf.frequency) if earliest_setf is not None else 0.0

from quantify_scheduler.resources import ClockResource



def _add_clock_resources_first(schedule: Schedule, channel_registry: QuantifyChannelRegistry) -> None:
    try:
        existing = set(schedule.resources.keys())
    except Exception:
        existing = set()

    for channel in channel_registry.values():
        if channel.clock in existing:
            continue
        init_freq = _infer_clock_freq_for_channel(channel)
        schedule.add_resource(ClockResource(name=channel.clock, freq=init_freq))
        existing.add(channel.clock)




def _infer_clock_freq_for_channel(channel: "QuantifyChannel") -> float:
    """
    For RF modules: ClockResource freq must be an ABSOLUTE RF frequency close to LO.
    We therefore use the earliest 'setf' on that clock.

    Quick-fix behavior:
    - If a setf has no t0, we still allow it (but can't order it by time).
    - If there is no setf at all, return a placeholder instead of raising.
    """
    # baseband clock can be 0
    if channel.clock.endswith(".baseband") or channel.clock == "cl0.baseband":
        return 0.0

    setf_inst = None
    earliest_t0 = None

    for inst in getattr(channel, "instructions", ()):
        if getattr(inst, "name", None) != "setf" or not hasattr(inst, "frequency"):
            continue

        t0 = getattr(inst, "t0", None)

        # If t0 is missing/invalid, keep it as a fallback candidate
        if t0 is None:
            if setf_inst is None:
                setf_inst = inst
            continue

        try:
            t0_val = float(t0)
        except (TypeError, ValueError):
            if setf_inst is None:
                setf_inst = inst
            continue

        if earliest_t0 is None or t0_val < earliest_t0:
            earliest_t0 = t0_val
            setf_inst = inst

    # No setf: assume nothing runs on this channel -> placeholder
    if setf_inst is None:
        log.debug(
            "No 'setf' found for clock '%s'. Using placeholder freq=%s Hz.",
            channel.clock,
            PLACEHOLDER_CLOCK_FREQ_HZ,
        )
        return PLACEHOLDER_CLOCK_FREQ_HZ

    return float(getattr(setf_inst, "frequency"))





def _snap_up_to_grid(dt: float, grid: float) -> float:
    """Snap a positive dt up to the next multiple of grid."""
    if dt <= 0:
        return 0.0
    return math.ceil(dt / grid) * grid

def _clock_qubit_id(clock: str) -> Optional[str]:
    # q16.01, q16.ro, q16.ro1, q16.ro_2st_opt -> "q16"
    if not clock.startswith("q"):
        return None
    return clock.split(".", 1)[0]

def _is_drive_clock(clock: str) -> bool:
    # qXX.01 or qXX.12
    parts = clock.split(".")
    return len(parts) == 2 and parts[1] in {"01", "12"}

def _is_readout_clock(clock: str) -> bool:
    # qXX.ro, qXX.ro1, qXX.ro_2st_opt, ...
    parts = clock.split(".")
    return len(parts) == 2 and parts[1].startswith("ro")

def _enforce_drive_to_readout_guard_time(
    channel_registry: QuantifyChannelRegistry,
    guard_time: float = _READOUT_GUARD_TIME,
    grid: float = QBLOX_TIMEGRID_INTERVAL,
) -> None:
    """
    Ensure for each qubit: first readout op starts >= (last MW drive end + guard_time).
    If not, shift ALL instructions on that readout clock forward (snapped to grid).
    """
    # 1) compute last MW drive end time per qubit
    last_drive_end: Dict[str, float] = {}

    for ch in channel_registry.values():
        if not _is_drive_clock(ch.clock):
            continue
        q = _clock_qubit_id(ch.clock)
        if q is None:
            continue

        # consider only "real" duration ops (pulses/delays) on the drive channel
        ends = [
            float(inst.final_timestamp)
            for inst in ch.instructions
            if getattr(inst, "duration", 0.0) and float(getattr(inst, "duration", 0.0)) > 0.0
        ]
        if not ends:
            continue

        last_drive_end[q] = max(last_drive_end.get(q, 0.0), max(ends))

    # 2) shift readout channels if they start too early
    for ro_ch in channel_registry.values():
        if not _is_readout_clock(ro_ch.clock):
            continue
        q = _clock_qubit_id(ro_ch.clock)
        if q is None or q not in last_drive_end:
            continue

        # earliest scheduled readout-related instruction time (ignore pure frequency controls)
        ro_times = [
            float(inst.t0)
            for inst in ro_ch.instructions
            if getattr(inst, "name", None) not in _FREQ_CONTROL_NAMES
        ]
        if not ro_times:
            continue

        ro_start = min(ro_times)
        required_start = last_drive_end[q] + guard_time

        if ro_start < required_start:
            shift = _snap_up_to_grid(required_start - ro_start, grid)

            # shift everything on that readout clock (including acquire) consistently
            for inst in ro_ch.instructions:
                inst.t0 = float(inst.t0) + shift



def _add_phase_resets_first(schedule: Schedule, channel_registry: QuantifyChannelRegistry) -> None:
    # Reset phases for all drive clocks at the start (qXX.01 / qXX.12)
    for ch in channel_registry.values():
        if ch.clock.endswith(".01") or ch.clock.endswith(".12"):
            schedule.add(
                ResetClockPhase(clock=ch.clock),
                label=f"reset_phase_{ch.clock}",
            )