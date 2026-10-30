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
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import uuid4 as uuid

import numpy as np

# Instead of constructing bare Operation objects, we now import the new API classes:
from quantify_scheduler import Operation
from quantify_scheduler.enums import BinMode
from quantify_scheduler.operations.acquisition_library import (
    SSBIntegrationComplex,
    Trace,
)
from quantify_scheduler.operations.pulse_library import (
    DRAGPulse,
    IdlePulse,
    NumericalPulse,
    SetClockFrequency,
    ShiftClockPhase,
    SoftSquarePulse,
    SquarePulse,
)

from app.libs.qiskit.qobj import PulseQobjConfig, PulseQobjInstruction
from app.libs.quantum_executor.base.quantum_job.dtos import NativeQobjConfig
from app.libs.quantum_executor.quantify.channel import (
    QuantifyChannel,
    QuantifyChannelRegistry,
)

QBLOX_TIMEGRID_INTERVAL = 4e-9
DT_CONST = 1e-9
INITIAL_RESET_VALUE = 500e-6


@dataclass(kw_only=True)
class BaseInstruction(abc.ABC):
    """An instruction on a channel, whose playback the channel registry adds it to"""

    name: str
    t0: float
    channel: QuantifyChannel
    duration: float
    port: Optional[str] = None
    frequency: Optional[float] = None
    phase: Optional[float] = None
    memory_slot: Optional[int] = None
    protocol: Optional[str] = None
    parameters: dict = field(default_factory=dict)
    pulse_shape: Optional[str] = None
    bin_mode: Optional[BinMode] = None
    acq_return_type: Optional[type] = None
    # unique to each instruction, so never compared
    label: str = field(default_factory=lambda: str(uuid()), compare=False)
    # its index among its channel's instructions, once added
    position: Optional[int] = None

    def get_phase_delta(self, channel: QuantifyChannel) -> float:
        """A representation of the change in phase this instruction introduces to its channel"""
        return 0

    def get_frequency_delta(self, channel: QuantifyChannel) -> float:
        """A representation of the change in frequency this instruction introduces to its channel"""
        return 0

    def get_acquisitions_delta(self, channel: QuantifyChannel) -> int:
        """A representation of the change in acquisitions this instruction introduces to its channel"""
        return 0

    @abc.abstractmethod
    def to_operation(self, config: PulseQobjConfig) -> Operation:
        """Gets the equivalent Operation for this instruction on the associated channel

        Args:
            config: the PulseQobjConfig corresponding to the parent experiment of this instruction

        Returns:
            the Operation generated for this instruction
        """

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


@dataclass(kw_only=True)
class InitialObjectInstruction(BaseInstruction):
    """The idle that a schedule's other operations start after"""

    name: str = field(default="initial_object", init=False)
    t0: float = 0.0
    channel: QuantifyChannel = field(
        default_factory=lambda: QuantifyChannel(clock="cl0.baseband")
    )
    duration: float = INITIAL_RESET_VALUE

    @classmethod
    def list_from_qobj_inst(
        cls,
        qobj_inst: PulseQobjInstruction,
        channel_registry: QuantifyChannelRegistry,
        **kwargs,
    ) -> List["InitialObjectInstruction"]:
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.duration * DT_CONST)
        channel = channel_registry[qobj_inst.ch]
        return [
            cls(
                t0=t0,
                channel=channel,
                duration=duration,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # Use the new IdlePulse operation to represent a no-op delay
        op = IdlePulse(duration=self.duration)
        return op


@dataclass(kw_only=True)
class AcquireInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'acquire'"""

    name: str = field(default="acquire", init=False)

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
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.duration * DT_CONST)
        acquire_instructions = []
        for n, qubit_idx in enumerate(qobj_inst.qubits):
            # FIXME: this assumes that measurement channel is always f"m{qubit_idx}". Could this assumption be wrong?
            qobj_channel = f"m{qubit_idx}"
            clock_name, port_name = hardware_map[qobj_channel]
            acquire_instructions.append(
                cls(
                    t0=t0,
                    channel=channel_registry[clock_name],
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
            )
        elif self.protocol == "trace":
            op = Trace(
                port=self.port,
                clock=self.channel.clock,
                duration=self.duration,
                acq_channel=int(self.channel.clock.split(".")[0][1:]),
                acq_index=acq_index,
            )
        else:
            raise RuntimeError(f"Unknown acquisition protocol {self.protocol}.")
        return op


@dataclass(kw_only=True)
class DelayInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'delay'"""

    name: str = field(default="delay", init=False)

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
        channel = channel_registry[clock_name]
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        duration = _map_to_qblox_timegrid(qobj_inst.duration * DT_CONST)
        return [
            cls(
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


@dataclass(kw_only=True)
class SetFreqInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'setf'"""

    name: str = field(default="setf", init=False)

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
        channel = channel_registry[clock_name]
        frequency_hz = qobj_inst.frequency * 1e9
        t0 = _map_to_qblox_timegrid(qobj_inst.t0 * DT_CONST)
        return [
            cls(
                channel=channel,
                port=port_name,
                duration=0.0,
                frequency=frequency_hz,
                t0=t0,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # Frequency after this instruction (playback already computed)
        freq_now = self.channel.get_freq_at_position(self.position)
        return SetClockFrequency(
            clock=self.channel.clock,
            clock_freq_new=freq_now,
        )


@dataclass(kw_only=True)
class ShiftFreqInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name 'shiftf'"""

    name: str = field(default="shiftf", init=False)

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
        channel = channel_registry[clock_name]
        frequency = qobj_inst.frequency
        return [
            cls(
                channel=channel,
                port=port_name,
                duration=0.0,
                frequency=frequency,
                t0=t0,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # After shiftf, the playback contains the updated absolute frequency.
        freq_now = self.channel.get_freq_at_position(self.position)
        return SetClockFrequency(
            clock=self.channel.clock,
            clock_freq_new=freq_now,
        )


@dataclass(kw_only=True)
class SetPhaseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with names 'setp'"""

    name: str = field(default="setp", init=False)

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
        channel = channel_registry[clock_name]
        # phase_degree = qobj_inst.phase * 180 / np.pi
        phase_degree = math.degrees(float(qobj_inst.phase))
        phase_degree = round(phase_degree, 12)
        return [
            cls(
                channel=channel,
                port=port_name,
                duration=0.0,
                phase=phase_degree,
                t0=t0,
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


@dataclass(kw_only=True)
class ShiftPhaseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with names 'fc'"""

    name: str = field(default="fc", init=False)

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
        channel = channel_registry[clock_name]
        phase_degree = qobj_inst.phase * 180 / np.pi
        drive_frequencies = kwargs.get("drive_frequencies") or {}
        lo_frequencies = kwargs.get("lo_frequencies") or {}
        drive_frequency_hz = drive_frequencies.get(clock_name)
        lo_frequency_hz = lo_frequencies.get(f"{port_name}-{clock_name}")
        if (
            drive_frequency_hz is not None
            and lo_frequency_hz is not None
            and lo_frequency_hz < drive_frequency_hz
        ):
            phase_degree = -phase_degree

        return [
            cls(
                channel=channel,
                port=port_name,
                duration=0.0,
                phase=phase_degree,
                t0=t0,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        """
        Send shift clock phase command in degree.
        """
        return ShiftClockPhase(
            phase_shift=self.phase,
            clock=self.channel.clock,
        )


@dataclass(kw_only=True)
class GaussPulseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction for parametric pulse with gaussian shape."""

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
        channel = channel_registry[clock_name]
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
        motzoi = self.parameters.get("beta", 0.0)
        op = DRAGPulse(
            G_amp=G_amp.real,
            D_amp=motzoi,
            duration=self.duration,
            port=self.port,
            clock=self.channel.clock,
            phase=phase,
            reference_magnitude=self.parameters.get("reference_magnitude"),
        )
        return op


@dataclass(kw_only=True)
class SquarePulseInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction for parametric pulse with square shape."""

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
        channel = channel_registry[clock_name]
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
        amp = self.parameters.get("amp")
        op = SquarePulse(
            amp=amp,
            duration=self.duration,
            port=self.port,
            clock=self.channel.clock,
            reference_magnitude=self.parameters.get("reference_magnitude"),
        )
        return op


@dataclass(kw_only=True)
class PulseLibInstruction(BaseInstruction):
    """Instructions from PulseQobjInstruction with name in pulse config library"""

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
        channel = channel_registry[clock_name]
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
            return _get_numerical_pulse(
                channel=self.channel, instruction=self, waveform=waveform
            )
        except KeyError:
            raise RuntimeError(f"Unable to schedule operation {self}.")


@dataclass(kw_only=True)
class WacqtCZPulseInstruction(BaseInstruction):
    """
    Microwave-frequency flux pulse for the WACQT-CZ gate.
    •  The seconds-scale DC sweet-spot bias is *not* scheduled here.
    •  `parameters` must contain `duration` (int samples) and `dc_bias` (A).
    """

    name: str = field(default="wacqt_cz", init=False)

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
        channel = channel_registry[clock]

        return [
            cls(
                t0=t0,
                channel=channel,
                port=port,
                duration=duration,
                parameters=params,
            )
        ]

    def to_operation(self, config: PulseQobjConfig) -> Operation:
        # TODO: assert duration == t_p or replace t_p, t_w, t_rf with one duration parameter
        return SoftSquarePulse(
            duration=self.duration,
            amp=float(self.parameters.get("delta_0")),
            port=self.port,
            clock=self.channel.clock,
        )


def _get_numerical_pulse(
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

    ticks = int(round(raw_time / grid_interval))
    return ticks * grid_interval
