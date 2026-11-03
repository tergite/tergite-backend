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
"""The WACQT CZ pulse, which the qiskit-dynamics simulators' circuit schedules play"""
from typing import SupportsIndex

import numpy as np
from qiskit import pulse as qiskit_pulse
from qiskit.pulse.channels import PulseChannel

from libs.quantum_executor.qiskit.functions import omega_c


class WacqtCZPlay(qiskit_pulse.Play):
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
