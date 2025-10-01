# This code is part of Tergite
#
# (C) Stefan Hill (2024)
# (C) Pontus Vikstål (2024)
# (C) Chalmers Next Labs (2024)
# (C) Martin Ahindura (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
import io
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Type

import qiskit.qpy as qpy
from qiskit.pulse.schedule import Schedule
from qiskit.qobj import PulseQobjConfig, PulseQobjExperiment, PulseQobjInstruction

from app.libs.quantum_executor.base.experiment import (
    NativeExperiment,
    copy_expt_header_with,
)

from ...device_parameters import get_backend_config
from .instruction import (
    Acquire,
    Delay,
    GaussianPlay,
    QiskitDynamicsInstruction,
    SetFrequency,
    SetPhase,
    ShiftFrequency,
    ShiftPhase,
    WacqtCZPlay,
)

# Map (name, pulse_shape) => QiskitDynamicsInstruction
_INSTRUCTION_PULSE_MAP: Dict[
    Tuple[str, Optional[str]], Type[QiskitDynamicsInstruction]
] = {
    ("setf", None): SetFrequency,
    ("shiftf", None): ShiftFrequency,
    ("setp", None): SetPhase,
    ("fc", None): ShiftPhase,
    ("delay", None): Delay,
    ("parametric_pulse", "constant"): Acquire,
    ("parametric_pulse", "gaussian"): GaussianPlay,
    ("parametric_pulse", "wacqt_cz_gate_pulse"): WacqtCZPlay,
}


@dataclass(frozen=True)
class QiskitDynamicsExperiment(NativeExperiment[Schedule]):

    @classmethod
    def from_qobj_expt(
        cls,
        expt: PulseQobjExperiment,
        name: str,
        qobj_config: PulseQobjConfig,
    ) -> "QiskitDynamicsExperiment":
        """Converts PulseQobjExperiment to qiskit dynamics experiment

        Args:
            expt: the pulse qobject experiment to translate
            name: the name of the experiment
            qobj_config: the pulse qobject config

        Returns:
            the QiskitDynamicsExperiment corresponding to the PulseQobj
        """
        header = copy_expt_header_with(expt.header, name=name)
        timestamp: str = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        schedule = Schedule(name=f"open-pulse-generated-{timestamp}")

        # native_instructions: List[QiskitDynamicsInstruction] = []
        qobj_instructions: List[PulseQobjInstruction] = expt.instructions

        for inst in qobj_instructions:
            try:
                native_inst = _to_native_instruction(inst)
                # native_instructions.append(native_inst)
                schedule = schedule.insert(inst.t0, native_inst)
            except NotImplementedError as exp:
                # FIXME: For now ignore all missing pulse shapes
                logging.error(f"NotImplementError for expt: {name}: {exp}")

        # compute estimated duration
        backend_conf = get_backend_config()
        time_step_len = backend_conf.general_config.dt
        duration = schedule.duration * time_step_len

        return cls(
            header=header,
            config=qobj_config,
            schedule=schedule,
            duration=duration,
        )


def _to_native_instruction(
    qobj_inst: PulseQobjInstruction,
) -> QiskitDynamicsInstruction:
    """Extracts qiskit pulse instruction from the PulseQobjInstruction

    Args:
        qobj_inst: the PulseQobjInstruction from which instructions are to be extracted

    Returns:
        the native qiskit instruction
    """
    name = qobj_inst.name
    pulse_shape = getattr(qobj_inst, "pulse_shape", None)

    try:
        native_inst_cls = _INSTRUCTION_PULSE_MAP[(name, pulse_shape)]
        return native_inst_cls.from_qobj(qobj_inst)
    except KeyError as exp:
        raise NotImplementedError(
            f"No mapping for PulseQobjInstruction {qobj_inst}.\n {exp}"
        )


def _schedule_to_bytes(schedule: Schedule) -> bytes:
    """Converts the schedule into a list of bytes that can be pickled

    Args:
        schedule: the schedule to serialize

    Returns:
        bytes got from serializing the schedule
    """
    buf = io.BytesIO()
    qpy.dump([schedule], buf)
    return buf.getvalue()


def _bytes_to_schedule(value: bytes) -> Schedule:
    """Converts the bytes into a schedule

    Args:
        value: the bytes to parse

    Returns:
        the schedule got after serialization
    """
    buf = io.BytesIO(value)
    [schedule] = qpy.load(buf)
    return schedule
