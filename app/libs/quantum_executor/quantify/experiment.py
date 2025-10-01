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

from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Type

from qiskit.qobj import (
    PulseQobjConfig,
    PulseQobjExperiment,
    PulseQobjInstruction,
    QobjExperimentHeader,
)
from quantify_scheduler import Schedule
from quantify_scheduler.resources import ClockResource
from quantify_scheduler.operations.pulse_library import IdlePulse


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
        instruction.register()

def _construct_schedule(
    channel_registry: QuantifyChannelRegistry,
    header: QobjExperimentHeader,
    config: PulseQobjConfig,
    timegrid_interval: float = QBLOX_TIMEGRID_INTERVAL,
) -> Schedule:
    """Constructs a schedule given

    Args:
        channel_registry: the iterable of QuantifyChannel's to which are attached ClockResource's
        header: the qobj experiment header
        config: the pulse qobject config
        timegrid_interval: the interval between grid lines in the time grid used by Q1ASM
    """
    raw_schedule = Schedule(name=header.name, repetitions=config.shots)

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
        if len(channel.instructions) == 1 and channel.instructions[0].name == "delay":
            # if the channel contains a single instruction and that instruction is a delay,
            # then do not schedule any operations on that channel
            print("\nNO DELAY\n")
            continue

        prev = root_instruction
        for curr in channel.instructions:
            rel_time = curr.t0 - prev.final_timestamp + timegrid_interval
            ref_op = prev.label

            raw_schedule.add(
                ref_op=ref_op,
                ref_pt="end",
                ref_pt_new="start",
                rel_time=rel_time,
                label=curr.label,
                operation=curr.to_operation(config=config),
            )

            # set the previous to the current
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

    return _get_absolute_timed_schedule(
        schedule=raw_schedule, channel_registry=channel_registry
    )


def _get_absolute_timed_schedule(
    schedule: Schedule, channel_registry: QuantifyChannelRegistry
) -> Schedule:
    """Returns a new schedule with absolute timing

    Args:
        schedule: the raw schedule to compile
        channel_registry: the iterable of QuantifyChannel's to which are attached ClockResource's

    Returns:
        the schedule with absolute time for each operation has been
        determined.
    """
    for channel in channel_registry.values():
        clock = ClockResource(name=channel.clock, freq=channel.final_frequency)
        schedule.add_resource(clock)

    return schedule
