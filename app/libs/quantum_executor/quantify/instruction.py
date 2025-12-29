# This code is part of Tergite
#
# (C) Axel Andersson (2022)
# (C) Martin Ahindura (2025)
# (C) Chalmers Next Labs (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import abc
from typing import Any, Dict, List, Optional
from uuid import uuid4 as uuid

import numpy as np
from qiskit.pulse.library import discrete as qiskit_discrete_lib
from qiskit.qobj import PulseQobjConfig, PulseQobjInstruction

# Instead of constructing bare Operation objects, we now import the new API classes:
from quantify_scheduler import Operation
from quantify_scheduler.enums import BinMode
from quantify_scheduler.operations.acquisition_library import (
    SSBIntegrationComplex,
    Trace,
)
from quantify_scheduler.operations.pulse_library import (
    GaussPulse,
    IdlePulse,
    NumericalPulse,
    SetClockFrequency,
    ShiftClockPhase,
    SoftSquarePulse,
    DRAGPulse,
    SquarePulse,
    ResetClockPhase
)

from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from app.libs.quantum_executor.qiskit.functions import _delta_t_function
from app.libs.quantum_executor.quantify.channel import (
    QuantifyChannel,
    QuantifyChannelRegistry,
)

QBLOX_TIMEGRID_INTERVAL = 4e-9
DT_CONST = 1e-9

def _qobj_freq_to_hz(freq: float) -> float:
    """
    Robust conversion:
    - If it looks like GHz (e.g., 4-8), convert to Hz.
    - If it looks like Hz already (e.g., 4e9), keep it.
    """
    f = float(freq)
    return f * 1e9 if abs(f) < 1e6 else f


def _prev_phase_before(inst: "BaseInstruction") -> float:
    """
    Find the phase that was active immediately before this instruction, based on
    playback and registration order on the same channel.

    IMPORTANT: We skip frequency-only instructions when reasoning about phase.
    """
    ch = inst.channel
    if inst.position <= 0:
        return 0.0

    # Walk backwards to find previous instruction that affects phase
    for pos in range(inst.position - 1, -1, -1):
        try:
            # phase playback at that position already includes that instruction's effect.
            # But we want the phase AFTER that previous instruction, which is playback[pos].
            return ch.get_phase_at_position(pos)
        except Exception:
            continue
    return 0.0

"""
Qblox instruments send pulses in a given equidistant time grid.
See https://docs.qblox.com/en/main/cluster/q1_sequence_processor.html#acquisitions for example  

Or see the table of Q1ASM instructions at
https://docs.qblox.com/en/main/cluster/q1_sequence_processor.html#q1-instructions,
where the execution time is always a multiple of 4 as of the time of writing this code.
"""


class BaseInstruction:
    __slots__ = (
        "t0",
        "name",
        "channel",
        "port",
        "duration",
        "frequency",
        "phase",
        "memory_slot",
        "protocol",
        "parameters",
        "pulse_shape",
        "bin_mode",
        "acq_return_type",
        "label",
        "position",
    )

    t0: float
    name: str
    channel: QuantifyChannel
    port: str
    duration: float
    frequency: float
    phase: float
    memory_slot: List[int]
    protocol: str
    parameters: dict
    pulse_shape: str
    bin_mode: BinMode
    acq_return_type: type
    label: str
    position: int

    def __init__(self, **kwargs):
        self.label = str(uuid())
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __eq__(self, other: object) -> bool:
        self_attrs = set(filter(lambda v: hasattr(self, v), BaseInstruction.__slots__))
        other_attrs = set(
            filter(lambda v: hasattr(other, v), BaseInstruction.__slots__)
        )

        # if they have different attributes, they cannot be equal
        if self_attrs != other_attrs:
            return False

        # label is always unique
        attrs = self_attrs - {"label"}

        # if they have the same attributes, they must also all have the same values
        for attr in attrs:
            if getattr(self, attr) != getattr(other, attr):
                return False

        # otherwise, they are the same
        return True

    def __repr__(self) -> str:
        repr_list = [f"BaseInstruction object @ {hex(id(self))}:"]
        for attr in BaseInstruction.__slots__:
            if hasattr(self, attr):
                repr_list.append(f"\t{attr} : {getattr(self, attr)}".expandtabs(4))
        return "\n".join(repr_list)

    @property
    def unique_name(self):
        return f"{self.pretty_name}-{self.channel.clock}-{round(self.t0 * 1e9)}"

    @property
    def pretty_name(self) -> str:
        return self.name

    @property
    def final_timestamp(self) -> float:
        """The final timestamp after the duration of this instruction"""
        return self.t0 + self.duration

    def get_phase_delta(self, channel: QuantifyChannel) -> float:
        """A representation of the change in phase this instruction introduces to its channel"""
        return 0

    def get_frequency_delta(self, channel: QuantifyChannel) -> float:
        """A representation of the change in frequency this instruction introduces to its channel"""
        return 0

    def get_acquisitions_delta(self, channel: QuantifyChannel) -> int:
        """A representation of the change in acquisitions this instruction introduces to its channel"""
        return 0

    def register(self):
        """Registers itself on its channel, updating its position.

        Its position is its index in the list of instructions attached to the channel
        """
        self.position = self.channel.register_instruction(self)

    @abc.abstractmethod
    def to_operation(self, config: PulseQobjConfig) -> Operation:
        """Gets the equivalent Operation for this instruction on the associated channel

        Args:
            config: the PulseQobjConfig corresponding to the parent experiment of this instruction

        Returns:
            the Operation generated for this instruction
        """
        pass

    @classmethod
    @abc.abstractmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        config: PulseQobjConfig,
        native_config: NativeQobjConfig,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
    ) -> List["BaseInstruction"]:
        """Generates instances of instruction given a PulseQobjInstruction

        Args:
            qobj_inst: the PulseQobjInstruction to convert from
            config: the PulseQobjConfig for the instruction
            native_config: the native configuration for the qobj
            channel_registry: the registry of channels for the current experiment
            hardware_map: the mapping of the layout of the physical device

        Returns:
            instances of this class as derived from the qobj_inst
        """


class InitialObjectInstruction(BaseInstruction):
    __slots__ = ()

    def __init__(
        self,
        channel: QuantifyChannel = QuantifyChannel(clock="cl0.baseband"),
        t0=0.0,
        duration=0.0,
        **kwargs,
    ):
        kwargs["name"] = "initial_object"
        super().__init__(t0=t0, channel=channel, duration=duration, **kwargs)

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        channel_registry: QuantifyChannelRegistry,
        **kwargs,
    ) -> List["InitialObjectInstruction"]:
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.duration * DT_CONST)
        channel = channel_registry.get(qobj_inst.ch)
        return [
            cls(
                t0=t0,
                channel=channel,
                duration=duration,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # Use the new IdlePulse operation to represent a no-op delay
        # op = IdlePulse(duration=self.duration)
        op = IdlePulse(duration=1000e-6)
        return op


class AcquireInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'acquire'"""

    __slots__ = ()

    def __init__(self, **kwargs):
        kwargs["name"] = "acquire"
        super().__init__(**kwargs)

    @property
    def pretty_name(self) -> str:
        return self.protocol

    def get_acquisitions_delta(self, channel: QuantifyChannel) -> int:
        return 1

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        native_config: NativeQobjConfig,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> List["AcquireInstruction"]:
        name = qobj_inst.name
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.duration * DT_CONST)
        acquire_instructions = []
        for n, qubit_idx in enumerate(qobj_inst.qubits):
            qobj_channel = f"m{qubit_idx}"
            clock_name, port_name = hardware_map[qobj_channel]
            acquire_instructions.append(
                cls(
                    name=name,
                    t0=t0,
                    channel=channel_registry.get(clock_name),
                    port=port_name,
                    duration=duration,
                    memory_slot=qobj_inst.memory_slot[n],
                    protocol=native_config.protocol.value,
                    acq_return_type=native_config.acq_return_type,
                    bin_mode=native_config.bin_mode,
                )
            )
        return acquire_instructions

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        acq_index = self.channel.get_acquisitions_at_position(self.position) - 1
        if self.protocol == "SSBIntegrationComplex":
            op = SSBIntegrationComplex(
                port=self.port,
                clock=self.channel.clock,
                duration=self.duration,
                acq_channel=int(self.channel.clock.split(".")[0][1:]),
                acq_index=acq_index,
                bin_mode=self.bin_mode,
                # t0=self.t0
            )
        elif self.protocol == "trace":
            op = Trace(
                port=self.port,
                clock=self.channel.clock,
                duration=self.duration,
                acq_channel=int(self.channel.clock.split(".")[0][1:]),
                acq_index=acq_index,
                t0=self.t0,
            )
        else:
            raise RuntimeError(f"Unknown acquisition protocol {self.protocol}.")
        return op


class DelayInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'delay'"""

    __slots__ = ()

    def __init__(self, **kwargs):
        kwargs["name"] = "delay"
        super().__init__(**kwargs)

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> List["DelayInstruction"]:
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.duration * DT_CONST)
        return [
            cls(
                name=qobj_inst.name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=duration,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # Idle pulse don't have t0 parameter
        op = IdlePulse(duration=self.duration)
        return op


class SetFreqInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'setf'"""

    __slots__ = ()

    def __init__(self, **kwargs):
        kwargs["name"] = "setf"
        super().__init__(**kwargs)

    def get_frequency_delta(self, channel: QuantifyChannel) -> float:
        # reset the channel frequency to zero then add the instruction frequency
        return self.frequency - channel.final_frequency

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> List["SetFreqInstruction"]:
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)
        frequency = _qobj_freq_to_hz(qobj_inst.frequency)
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        return [
            SetFreqInstruction(
                name=qobj_inst.name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=0.0,
                frequency=frequency,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # Frequency after this instruction (playback already computed)
        freq_now = self.channel.get_freq_at_position(self.position)
        return SetClockFrequency(
            clock=self.channel.clock,
            clock_freq_new=freq_now,
        )


class ShiftFreqInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'shiftf'"""

    __slots__ = ()

    def __init__(self, **kwargs):
        kwargs["name"] = "shiftf"  # 'shiftf' does not work apparently
        super().__init__(**kwargs)

    def get_frequency_delta(self, channel: QuantifyChannel) -> float:
        return self.frequency

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> List["ShiftFreqInstruction"]:
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)
        frequency = _qobj_freq_to_hz(qobj_inst.frequency)
        return [
            ShiftFreqInstruction(
                name=qobj_inst.name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=0.0,
                frequency=frequency,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # After shiftf, the playback contains the updated absolute frequency.
        freq_now = self.channel.get_freq_at_position(self.position)
        return SetClockFrequency(
            clock=self.channel.clock,
            clock_freq_new=freq_now,
        )


class SetPhaseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with names 'setp'"""

    __slots__ = ()

    def __init__(self, **kwargs):
        kwargs["name"] = "setp"
        super().__init__(**kwargs)

    def get_phase_delta(self, channel: QuantifyChannel) -> float:
        # reset the channel phase to zero then add the instruction phase
        return self.phase - channel.final_phase

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> List["SetPhaseInstruction"]:
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)
        return [
            SetPhaseInstruction(
                name=qobj_inst.name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=0.0,
                phase=qobj_inst.phase,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # phase after this instruction according to playback
        target = self.channel.get_phase_at_position(self.position)

        # phase before this instruction according to playback
        prev = 0.0
        if self.position > 0:
            prev = self.channel.get_phase_at_position(self.position - 1)

        # delta to apply now
        delta = target - prev

        return ShiftClockPhase(phase_shift=delta, clock=self.channel.clock)


class ShiftPhaseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with names 'fc'"""

    __slots__ = ()

    def __init__(self, **kwargs):
        kwargs["name"] = "fc"
        super().__init__(**kwargs)

    def get_phase_delta(self, channel: QuantifyChannel) -> float:
        return self.phase

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> List["ShiftPhaseInstruction"]:
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)
        return [
            ShiftPhaseInstruction(
                name=qobj_inst.name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=0.0,
                phase=qobj_inst.phase,
            )
        ]
            
    def to_operation(self, config: PulseQobjConfig) -> Operation:
        """
        Qiskit 'fc' is a relative frame change.
        Your playback already accumulated it, so delta = new - prev.
        """
        prev_phase = _prev_phase_before(self)
        new_phase = self.channel.get_phase_at_position(self.position)
        delta = new_phase - prev_phase

        return ShiftClockPhase(
            phase_shift=delta,
            clock=self.channel.clock,
        )


class GaussPulseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction for parametric pulse with gaussian shape."""

    __slots__ = ()

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        config: PulseQobjConfig,
        native_config: NativeQobjConfig,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
    ) -> List["GaussPulseInstruction"]:
        # Map timing and channel information as appropriate.
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.parameters["duration"] * DT_CONST)
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)
        return [
            cls(
                name=qobj_inst.name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=duration,
                pulse_shape=qobj_inst.pulse_shape,
                parameters=qobj_inst.parameters,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # Extract parameters, with defaults as needed.
        G_amp = self.parameters.get("amp")
        phase = self.parameters.get("phase", 0.0)
        sigma = self.parameters.get("sigma", self.duration / 4)
        op = DRAGPulse(
            G_amp=G_amp.real,
            D_amp=0,
            duration=self.duration,
            port=self.port,
            clock=self.channel.clock,
            phase=phase,
            # t0=self.t0,
            reference_magnitude=self.parameters.get("reference_magnitude"),
        )
        return op


class SquarePulseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction for parametric pulse with square shape."""

    __slots__ = ()

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        config: PulseQobjConfig,
        native_config: NativeQobjConfig,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
    ) -> List["SquarePulseInstruction"]:
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.parameters["duration"] * DT_CONST)
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)

        reset_clock = ResetClockPhase(
                clock=clock_name,
            )

        square = cls(
                name=qobj_inst.name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=duration,
                pulse_shape=qobj_inst.pulse_shape,
                parameters=qobj_inst.parameters,
            )
        return [inst for inst in (reset_clock, square) if inst is not None]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        amp = self.parameters.get("amp")
        op = SquarePulse(
            amp=amp,
            duration=self.duration,
            port=self.port,
            clock=self.channel.clock,
            # t0=self.t0,
            reference_magnitude=self.parameters.get("reference_magnitude")
        )
        return op


class ParamPulseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'parametric_pulse'"""

    __slots__ = ()

    def __init__(self, **kwargs):
        kwargs["name"] = "parametric_pulse"
        super().__init__(**kwargs)

    @property
    def pretty_name(self) -> str:
        return self.pulse_shape

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        config: PulseQobjConfig,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> List["ParamPulseInstruction"]:
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.parameters["duration"] * DT_CONST)
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)
        return [
            cls(
                name=qobj_inst.name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=duration,
                pulse_shape=qobj_inst.pulse_shape,
                parameters=qobj_inst.parameters,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        wf_fn = getattr(qiskit_discrete_lib, str.lower(self.pulse_shape))
        waveform = wf_fn(**self.parameters).samples
        return _generate_numerical_pulse(
            channel=self.channel, instruction=self, waveform=waveform
        )


class PulseLibInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name in pulse config library"""

    __slots__ = ()

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        config: PulseQobjConfig,
        native_config: NativeQobjConfig,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
    ) -> List["PulseLibInstruction"]:
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        qobj_channel = qobj_inst.ch
        clock_name, port_name = hardware_map[qobj_channel]
        channel = channel_registry.get(clock_name)
        name = qobj_inst.name
        # FIXME: pulse_library seems to be a list but is accessed as a dict
        pulse_duration = config.pulse_library[name].shape[0]
        duration = _map_to_qblox_timegrid(pulse_duration * DT_CONST)
        return [
            cls(
                name=name,
                t0=t0,
                channel=channel,
                port=port_name,
                duration=duration,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        try:
            # FIXME: pulse_library seems to be a list but is accessed here as a dict
            waveform = config.pulse_library[self.name]
            return _generate_numerical_pulse(
                channel=self.channel, instruction=self, waveform=waveform
            )
        except KeyError:
            raise RuntimeError(f"Unable to schedule operation {self}.")


class WacqtCZPulseInstruction(BaseInstruction):
    """
    Microwave-frequency flux pulse for the WACQT-CZ gate.
    •  The seconds-scale DC sweet-spot bias is *not* scheduled here.
    •  `parameters` must contain `duration` (int samples) and `dc_bias` (A).
    """

    __slots__ = ()

    def __init__(self, **kwargs):
        kwargs["name"] = "wacqt_cz"
        super().__init__(**kwargs)

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        config: PulseQobjConfig,
        native_config: NativeQobjConfig,
        channel_registry: QuantifyChannelRegistry,
        hardware_map: Optional[Dict[str, Any]] = None,
    ) -> List["WacqtCZPulseInstruction"]:
        # Accept either a dedicated name or a parametric pulse with shape tag
        is_name_ok = qobj_inst.name.lower() in {"wacqt_cz", "parametric_pulse"}
        shape = getattr(qobj_inst, "pulse_shape", "wacqt_cz")
        shape_ok = isinstance(shape, str) and shape.lower() in {
            "wacqt_cz",
            "wacqt_cz_gate_pulse",
        }
        if not (is_name_ok and shape_ok):
            return []
        params = {k.lower(): v for k, v in qobj_inst.parameters.items()}
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(params["duration"] * DT_CONST)

        clock, port = hardware_map[qobj_inst.ch]
        channel = channel_registry.get(clock)

        cz_freq = params.get("freq")

        setf = None
        if cz_freq is not None:
            setf = SetFreqInstruction(
                name="setf",
                # t0=t0,
                channel=channel,
                port=port,
                duration=0.0,
                frequency=float(cz_freq),
            )
        cz = cls(
            t0=t0,
            channel=channel,
            port=port,
            duration=duration,
            parameters=params,
        )

        return [inst for inst in (setf, cz) if inst is not None]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # TODO: assert duration == t_p or replace t_p, t_w, t_rf with one duration parameter
        # TODO: check if need to provide reference_magnitude=self.parameters.get("reference_magnitude"),
        return SoftSquarePulse(
            duration=float(self.parameters.get("t_p")),
            amp=float(self.parameters.get("delta_0")),
            port=self.port,
            clock=self.channel.clock,
            # t0=self.t0,
        )


def _generate_numerical_pulse(
    channel: QuantifyChannel, instruction: BaseInstruction, waveform: np.ndarray
) -> Operation:
    """Generates a numerical pulse on the given channel for the given instruction given a particular waveform

    Args:
        channel: the channel on which the pulse is to be sent
        instruction: the raw instruction
        waveform: the points that form the samples from which the numerical pulse is to be generated

    Returns:
        Operation representing the numerical pulse
    """

    current_phase = channel.get_phase_at_position(instruction.position)
    waveform = waveform * np.exp(1.0j * current_phase)
    t_samples = np.linspace(0, instruction.duration, len(waveform)).tolist()
    op = NumericalPulse(
        samples=waveform.tolist(),
        t_samples=t_samples,
        port=instruction.port,
        clock=instruction.channel.clock,
        # t0=instruction.t0,
        interpolation="linear",
    )
    return op


def _map_to_qblox_timegrid(
    raw_time: float, grid_interval: float = QBLOX_TIMEGRID_INTERVAL
) -> float:
    """Generates the timestamp within the qblox timestamp that corresponds to the raw_timestamp

    Qblox instruments send pulses in a given equidistant time grid.
    See https://docs.qblox.com/en/main/cluster/q1_sequence_processor.html#acquisitions for example
    or see the table of Q1ASM instructions at
    https://docs.qblox.com/en/main/cluster/q1_sequence_processor.html#q1-instructions,
    where the execution time is always a multiple of 4 as of the time of writing this code.

    Args:
        raw_time: the unmapped timestamp or duration
        grid_interval: the shortest possible time between two grid lines

    Returns:
        the timestamp or duration within the qblox time grid that corresponds to the given timestamp
    """

    time_to_next_gridline = (grid_interval - raw_time) % grid_interval
    return raw_time + time_to_next_gridline
