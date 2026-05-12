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
import abc
from abc import ABC
from typing import Optional, SupportsIndex, Union

import numpy as np
from qiskit import pulse as qiskit_pulse
from qiskit.circuit import ParameterExpression
from qiskit.pulse.channels import ControlChannel, DriveChannel, PulseChannel

from app.libs.qiskit.qobj import PulseQobjInstruction
from app.libs.quantum_executor.qiskit.functions import omega_c


class QiskitDynamicsInstruction(qiskit_pulse.Instruction, ABC):
    """Base instruction for qiskit dynamics instructions"""

    @classmethod
    @abc.abstractmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "QiskitDynamicsInstruction":
        """Generate an instance of this class given a PulseQobjInstruction

        Args:
            qobj_inst: the PulseQobjInstruction instance to generate from

        Returns:
            an instance of this class as generated from the qobj_inst
        """
        pass


class GaussianPlay(qiskit_pulse.Play, QiskitDynamicsInstruction):
    """A play instruction for Gaussian pulses only"""

    def __init__(
        self,
        channel: PulseChannel,
        duration: Union[int, ParameterExpression],
        amp: Union[ParameterExpression, float],
        sigma: Union[ParameterExpression, float],
        beta: Union[ParameterExpression, float, None] = None,
        angle: Union[ParameterExpression, float, None] = None,
        name: Optional[str] = None,
        limit_amplitude: Optional[bool] = None,
    ):
        """Create a new Gaussian pulse instruction.

        As copied from qiskit.pulse.Gaussian and qiskit.pulse.Play

        Args:
            channel: The channel to which the pulse is applied.
            duration: Pulse length in terms of the sampling period `dt`.
            amp: The magnitude of the amplitude of the Gaussian envelope.
                    Complex amp support is deprecated.
            sigma: A measure of how wide or narrow the Gaussian peak is; described mathematically
                   in the class docstring.
            angle: The angle of the complex amplitude of the Gaussian envelope. Default value 0.
            name: Display name for this pulse envelope.
            limit_amplitude: If ``True``, then limit the amplitude of the
                waveform to 1. The default is ``True`` and the amplitude is constrained to 1.
        """
        angle = 0.0 if angle is None else angle
        pulse = qiskit_pulse.Gaussian(
            duration=duration,
            amp=amp.real,
            sigma=sigma,
            angle=angle,
            name=name,
            limit_amplitude=limit_amplitude,
        )
        super().__init__(pulse, channel)

    @classmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "GaussianPlay":
        channel = _get_channel(qobj_inst)
        # amp values is no longer complex
        # TODO: find ref to the docs update
        # TODO: check with older client version
        return cls(
            channel=DriveChannel(channel),
            **qobj_inst.parameters,
        )


class WacqtCZPlay(qiskit_pulse.Play, QiskitDynamicsInstruction):
    """A play instruction for the WACQT_CZ custom gate"""

    def __init__(
        self,
        channel: PulseChannel,
        omega_c0: float,
        theta: float,
        omega_phi: float,
        phi: float,
        t_w: float,
        t_rf: float,
        t_p: float,
        delta_0: float,
        duration: SupportsIndex,
        **kwargs,
    ):
        """Creates a Play instruction to play the WACQT CZ gate custom pulse

        FIXME: Find out and document what each of these parameters are

        Args:
            duration: ...
            omega_c0: ...
            theta: ...
            omega_phi: ...
            phi: ...
            t_w: time for w ...
            t_rf: time for rf ...
            t_p: total time for p ...
            delta_0: ...

        """

        # total time of gate
        t_gate = t_p + t_rf + 2 * t_w

        # Generate the time array centered in each dt interval
        time_array = np.linspace(0, t_gate, duration)

        # Compute omega_c_array
        omega_c_for_time_array = omega_c(
            time_array,
            omega_c0=omega_c0,
            theta=theta,
            omega_phi=omega_phi,
            phi=phi,
            t_w=t_w,
            t_rf=t_rf,
            t_p=t_p,
            delta_0=delta_0,
        )
        omega_c_for_time_0 = omega_c(
            0,
            omega_c0=omega_c0,
            theta=theta,
            omega_phi=omega_phi,
            phi=phi,
            t_w=t_w,
            t_rf=t_rf,
            t_p=t_p,
            delta_0=delta_0,
        )
        omega_c_array = omega_c_for_time_array - omega_c_for_time_0

        pulse = qiskit_pulse.Waveform(samples=omega_c_array, limit_amplitude=False)
        super().__init__(pulse, channel)

    @classmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "WacqtCZPlay":
        channel = _get_channel(qobj_inst)
        normalized_params = {k.lower(): v for k, v in qobj_inst.parameters.items()}
        return cls(channel=ControlChannel(channel), **normalized_params)


class SetFrequency(qiskit_pulse.SetFrequency, QiskitDynamicsInstruction):
    """A custom set frequency instruction based on qiskit.pulse.SetFrequency"""

    @classmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "SetFrequency":
        if _is_measurement(qobj_inst):
            raise NotImplementedError("SetFrequency not implemented for measurement")

        channel = _get_channel(qobj_inst)
        return cls(frequency=qobj_inst.frequency * 1e9, channel=DriveChannel(channel))


class ShiftFrequency(qiskit_pulse.ShiftFrequency, QiskitDynamicsInstruction):
    """A custom shift frequency instruction based on qiskit.pulse.ShiftFrequency"""

    @classmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "ShiftFrequency":
        if _is_measurement(qobj_inst):
            raise NotImplementedError("ShiftFrequency not implemented for measurement")

        channel = _get_channel(qobj_inst)
        return cls(frequency=qobj_inst.frequency * 1e9, channel=DriveChannel(channel))


class SetPhase(qiskit_pulse.SetPhase, QiskitDynamicsInstruction):
    """A custom set phase instruction based on qiskit.pulse.SetPhase"""

    @classmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "SetPhase":
        if _is_measurement(qobj_inst):
            raise NotImplementedError("SetPhase not implemented for measurement")

        channel = _get_channel(qobj_inst)
        return cls(phase=qobj_inst.phase, channel=DriveChannel(channel))


class ShiftPhase(qiskit_pulse.ShiftPhase, QiskitDynamicsInstruction):
    """A custom shift phase instruction based on qiskit.pulse.ShiftPhase"""

    @classmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "ShiftPhase":
        if _is_measurement(qobj_inst):
            raise NotImplementedError("ShiftPhase not implemented for measurement")

        channel = _get_channel(qobj_inst)
        return cls(phase=qobj_inst.phase, channel=DriveChannel(channel))


class Delay(qiskit_pulse.Delay, QiskitDynamicsInstruction):
    """A custom delay instruction based on qiskit.pulse.Delay"""

    @classmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "Delay":
        params = getattr(qobj_inst, "parameters", {})
        duration = params.get("duration", getattr(qobj_inst, "duration", 0))
        channel = _get_channel(qobj_inst)
        return cls(
            duration=duration,
            channel=DriveChannel(channel),
        )


class Acquire(qiskit_pulse.Acquire, QiskitDynamicsInstruction):
    """A custom acquire instruction based on qiskit.pulse.Acquire"""

    @classmethod
    def from_qobj(cls, qobj_inst: PulseQobjInstruction) -> "Acquire":
        channel = _get_channel(qobj_inst)
        return cls(
            duration=1,  # set duration to any value, because it does not matter
            channel=qiskit_pulse.AcquireChannel(channel),
            mem_slot=qiskit_pulse.MemorySlot(channel),
        )


def _get_channel(instruction: PulseQobjInstruction) -> int:
    """Gets the channel from the given instruction

    Args:
        instruction: the instruciton from which to extract the channel

    Returns:
        the channel as an integer
    """
    return int(instruction.ch.strip("d").strip("readout").strip("m").strip("u"))


def _is_measurement(instruction: PulseQobjInstruction) -> bool:
    """Checks whether the given instruction is a measurement

    Args:
        instruction: the instruction to check

    Returns:
        True if the instruction is a measurement, else False
    """
    return instruction.name == "acquire" or instruction.ch.startswith(("readout", "m"))
