# This code is part of Tergite
#
# (C) Chalmers Next Labs (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.


from typing import Dict, List, Tuple

from quantify_scheduler.backends.qblox_backend import QbloxHardwareCompilationConfig


def generate_hardware_map(
    qubit_ids: List[str],
    coupling_dict: Dict[str, Tuple[str, str]],
    quantify_config: QbloxHardwareCompilationConfig,
) -> Dict[str, Tuple[str, str]]:
    """
    Create a mapping from the qobj port-clock convention to the QBLOX instrument configuration.

    Parameters:
      qubit_ids: List of qubit id strings, e.g. ["q6", "q7", "q8"].
      coupling_dict: Dictionary of coupling channels, e.g. { "u0": ("q6", "q7"), "u1": ("q6", "q8") }.
      quantify_config: QbloxHardwareCompilationConfig instance.
                       (Currently not parsed and future implementation will extract port/clock links.)

    Returns:
      A dictionary mapping channel keys to a tuple (clock, port). The keys for drive (d) and measurement (m)
      channels are generated by ordering the qubits (based on the numeric portion) so that the keys start from zero,
      irrespective of the starting qubit id. For coupling channels (u), the keys come directly from coupling_dict.

    Example output for:
      qubit_ids = ["q6", "q7", "q8"]
      coupling_dict = { "u0": ("q6", "q7"), "u1": ("q6", "q8") }

      {
          "d0": ("q06.01", "q06:mw"),
          "d1": ("q07.01", "q07:mw"),
          "d2": ("q08.01", "q08:mw"),
          "m0": ("q06.ro", "q06:res"),
          "m1": ("q07.ro", "q07:res"),
          "m2": ("q08.ro", "q08:res"),
          "u0": ("q06_q07.cz", "q06_q07:fl"),
          "u1": ("q06_q08.cz", "q06_q08:fl"),
      }
    """

    def pad_qubit(qubit: str) -> str:
        # Remove the 'q' prefix and pad if necessary.
        num = qubit.lstrip("q")
        return num if len(num) > 1 else f"0{num}"

    hardware_map: Dict[str, Tuple[str, str]] = {}

    # Sort qubit_ids based on the numeric portion so keys always start from 0.
    sorted_qubits = sorted(qubit_ids, key=lambda q: int(q.lstrip("q")))

    # Generate drive ("d") and measurement ("m") channels for each qubit.
    for idx, qubit in enumerate(sorted_qubits):
        padded = pad_qubit(qubit)
        # Drive channel:
        drive_clock = f"q{padded}.01"  # drive clock
        drive_port = f"q{padded}:mw"  # microwave port (drive)
        hardware_map[f"d{idx}"] = (drive_clock, drive_port)

        # Measurement channel:
        meas_clock = f"q{padded}.ro"  # readout clock
        meas_port = f"q{padded}:res"  # resonator port (measurement)
        hardware_map[f"m{idx}"] = (meas_clock, meas_port)

    # Generate coupling ("u") channels using the provided coupling_dict.
    for key, qubits in coupling_dict.items():
        # Expecting exactly two qubits in each coupling channel.
        if len(qubits) != 2:
            raise ValueError(f"Coupling channel '{key}' must have exactly two qubits.")
        padded_q1 = pad_qubit(qubits[0])
        padded_q2 = pad_qubit(qubits[1])
        # For coupling channels, use the convention: "qXX_qYY.cz" for clock and "qXX_qYY:fl" for port.
        coupling_clock = f"q{padded_q1}_q{padded_q2}.cz"
        coupling_port = f"q{padded_q1}_q{padded_q2}:fl"
        hardware_map[key] = (coupling_clock, coupling_port)

    return hardware_map
