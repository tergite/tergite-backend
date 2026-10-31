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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from libs.quantum_executor.quantify.instruction import BaseInstruction


class QuantifyChannelRegistry(dict):
    """A registry of channels accessible by their clock's

    To get a given channel, even if it does not exist yet,
    index the registry, e.g. registry["m0"]; get returns None for a missing one.

    If it does not exist,
    a new one with default final_phase=0, final_frequency=0, final_acquisitions=0,
    will be created and returned with the clock={key} in this case,
    key = 'm0'.
    """

    def __missing__(self, clock: str) -> "QuantifyChannel":
        channel = QuantifyChannel(clock=clock)
        self[clock] = channel
        return channel

    def add(self, instruction: "BaseInstruction") -> None:
        """Adds the instruction to its channel's playback, at the next position

        Args:
            instruction: the instruction to add; its position is set to where it is added
        """
        instruction.position = instruction.channel.register_instruction(instruction)


@dataclass(eq=True)
class QuantifyChannel:
    """The channel representing the physical port on which the instructions are ren"""

    clock: str
    _instructions: List["BaseInstruction"] = field(default_factory=list)
    _phase_playback: List[float] = field(default_factory=list)
    _frequency_playback: List[float] = field(default_factory=list)
    _acquisition_playback: List[int] = field(default_factory=list)

    @property
    def instructions(self) -> List["BaseInstruction"]:
        """List of instructions attached to this channel"""
        return self._instructions

    @property
    def final_phase(self) -> float:
        """The current phase after all attached instructions were run"""
        try:
            return self._phase_playback[len(self._instructions) - 1]
        except IndexError:
            return 0.0

    @property
    def final_frequency(self) -> float:
        """The current frequency after all attached instructions were run"""
        try:
            return self._frequency_playback[len(self._instructions) - 1]
        except IndexError:
            return 0.0

    @property
    def final_acquisitions(self) -> int:
        """The current acquisitions after all attached instructions were run"""
        try:
            return self._acquisition_playback[len(self._instructions) - 1]
        except IndexError:
            return 0

    def get_phase_at_position(self, instruction_position: int) -> float:
        """Get the phase at the given instruction_position

        This is important for idempotency if we wish to recompile particular instructions
        or compile them concurrently or something, we can easily do a playback at a given instance

        Args:
            instruction_position: the position in the list of instructions attached to this channel

        Returns:
            the phase at the instruction_position
        """
        return self._phase_playback[instruction_position]

    def get_freq_at_position(self, instruction_position: int) -> float:
        """Get the frequency at the given instruction_position

        This is important for idempotency if we wish to recompile particular instructions
        or compile them concurrently or something, we can easily do a playback at a given instance

        Args:
            instruction_position: the position in the list of instructions attached to this channel

        Returns:
            the frequency at the instruction_position
        """
        return self._frequency_playback[instruction_position]

    def get_acquisitions_at_position(self, instruction_position: int) -> int:
        """Get the acquisitions at the given instruction_position

        This is important for idempotency if we wish to recompile particular instructions
        or compile them concurrently or something, we can easily do a playback at a given instance

        Args:
            instruction_position: the position in the list of instructions attached to this channel

        Returns:
            the phase at the instruction_position
        """
        return self._acquisition_playback[instruction_position]

    def register_instruction(self, instruction: "BaseInstruction") -> int:
        """Registers an instruction to run on this channel

        Args:
            instruction: the instruction to register

        Returns:
            the position of that instruction in the list of instructions on this channel
        """
        index = len(self._instructions)

        phase_delta = instruction.get_phase_delta(channel=self)
        freq_delta = instruction.get_frequency_delta(channel=self)
        acquisitions_delta = instruction.get_acquisitions_delta(channel=self)

        self._phase_playback.append(self.final_phase + phase_delta)
        self._frequency_playback.append(self.final_frequency + freq_delta)
        self._acquisition_playback.append(self.final_acquisitions + acquisitions_delta)

        self._instructions.append(instruction)
        return index

    def __hash__(self: "QuantifyChannel") -> int:
        return hash(self.clock)
