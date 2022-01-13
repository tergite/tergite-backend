# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek, Andreas Bengtsson 2020,
# (C) Copyright Abdullah-Al Amin 2021
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.


from Labber import Scenario
from Labber import ScriptTools

import numpy as np
import json
from pathlib import Path
from tempfile import gettempdir


def demodulation_scenario(signal_array, demod_array):
    # create and add instruments
    s = Scenario()
    instr_signal = s.add_instrument("Simple Signal Generator", name="Sine")
    instr_demod = s.add_instrument("Signal Demodulation", name="Demod")

    # set a few instrument settings
    instr_demod.values["Use phase reference signal"] = False
    instr_demod.values["Length"] = 1.0

    # add signal connections between channels
    s.add_connection("Sine - Signal", "Demod - Input data")

    # add step items, values can be defined with np array or keywords
    s.add_step("Sine - Frequency", signal_array)
    s.add_step("Demod - Modulation frequency", demod_array)

    # add log channels
    s.add_log("Demod - Value")

    # set metadata
    s.comment = "Comment for log"
    s.tags.project = "My project"
    s.tags.user = "John Doe"
    s.tags.tags = ["Tag 1", "Tag 2/Subtag"]

    # set timing info
    s.wait_between = 0.01

    return s


def qobj_scenario(job):
    supported_gates = set(
        ["u1", "u2", "u3", "rx", "ry", "rz", "x", "y", "z", "h", "cz", "cx", "measure"]
    )
    scenario_template_filepath = Path("./qiskit_qasm_scenario_template.json")
    calibration_filepath = Path("./qiskit_qasm_calibration_config.json")

    qobj = job["params"]["qobj"]

    def validate_gate(gate):
        if gate["name"] not in supported_gates:
            raise ValueError("{} is not supported by the hardware".format(gate["name"]))

        if "conditional" in gate:
            raise ValueError("Conditional gates are not supported by the hardware")

    def validate_job(qobj):
        if qobj["type"] != "QASM":
            raise ValueError("Only QASM-type jobs are supported.")

    validate_job(qobj)
    for ins in qobj["experiments"][0]["instructions"]:
        validate_gate(ins)

    s = Scenario(scenario_template_filepath)

    mqpg = s.get_instrument(name="pulses")
    n_qubits = qobj["config"].get("n_qubits", 1)
    mqpg.values["Sequence"] = "QObj"
    mqpg.values["QObj JSON"] = json.dumps(qobj["experiments"])

    # configure number of shots
    update_step_single_value(s, "QA - Samples", qobj["config"].get("shots", 1024))

    # update with latest calibration data, if it exists
    if calibration_filepath.exists():
        with open(calibration_filepath, "r") as f:
            calibration = json.load(f)
        update_calibration_data(s, calibration)

    # Configure mulitple experiments
    if len(qobj["experiments"]) == 1:
        s.add_step("pulses - QObj Iterator", single=0)
    else:
        s.add_step("pulses - QObj Iterator", np.arange(len(qobj["experiments"])))

    # add relevant log channels
    extraction = job.get("hdf5_log_extraction", None)
    if extraction:
        if extraction.get("waveforms", False):
            add_waveforms(s, n_qubits)
        if extraction.get("voltages", False):
            add_readout_voltages(s, n_qubits)

    # set metadata
    s.log_name = "Test qobj"
    s.comment = "Comment for log"
    s.tags.project = "My project"
    s.tags.user = "Chalmers default user"
    s.tags.tags = ["Qobj"]

    # set timing info
    s.wait_between = 0.2

    return s


## A template scenario file is modified according to the parameters
## that have been passed through job object in order to create a simple
## frequency sweep scenario
def resonator_spectroscopy_scenario(job):
    VNA = "ZNB20"  # "RS"  # 'Keysight'  'Ceyear'

    scenario_template_filepath = Path("./resonator_spectroscopy_scenario_template.json")

    # loading Scenario as dictionary
    s_dict = ScriptTools.load_scenario_as_dict(scenario_template_filepath)

    s_prms = job["params"]

    # Updating Step parameters in Scenario dictionary
    for i, stepchannel in enumerate(s_dict["step_channels"]):
        # update: VNA - Output power
        if stepchannel["channel_name"] == VNA + " - Output power":
            s_dict["step_channels"][i]["step_items"][0]["single"] = s_prms["power"]
        # update VNA - IF bandwidth
        elif stepchannel["channel_name"] == VNA + " - IF bandwidth":
            s_dict["step_channels"][i]["step_items"][0]["single"] = s_prms["if_bw"]
        # update VNA - Number of averages
        elif stepchannel["channel_name"] == VNA + " - # of averages":
            s_dict["step_channels"][i]["step_items"][0]["single"] = s_prms["num_ave"]
        # update VNA - Start of Sweping frequency
        elif stepchannel["channel_name"] == VNA + " - Start frequency":
            s_dict["step_channels"][i]["step_items"][0]["single"] = s_prms["f_start"]
        # update VNA - Stop of Sweping frequency
        elif stepchannel["channel_name"] == VNA + " - Stop frequency":
            s_dict["step_channels"][i]["step_items"][0]["single"] = s_prms["f_stop"]
        # update VNA - Number of measurement points (data points)
        elif stepchannel["channel_name"] == VNA + " - # of points":
            s_dict["step_channels"][i]["step_items"][0]["single"] = s_prms["num_pts"]

    # Saving Scenario in a temporary file as JSON format
    temp_dir = gettempdir()
    ScriptTools.save_scenario_as_json(s_dict, temp_dir + "/tmp.json")

    # Loading Scenario as object
    s = Scenario(temp_dir + "/tmp.json")

    # set metadata
    s.log_name = "Resonator Spectroscopy"
    s.comment = "Comment for log"
    s.tags.project = "Automatic Calibration project"
    s.tags.user = "Chalmers default user"
    s.tags.tags = ["ResonatorSpectroscopy"]

    # set timing info
    s.wait_between = 0.2

    return s


def qobj_dummy_scenario(job):
    scenario_template_filepath = Path("./__stub__qiskit_qasm_scenario_template.json")

    qobj = job["params"]["qobj"]

    def validate_job(qobj):
        if qobj["type"] != "QASM":
            raise ValueError("Only QASM-type jobs are supported.")

    validate_job(qobj)
    s = Scenario(scenario_template_filepath)
    instr = s.get_instrument(name="State Discriminator")
    items = s.step_items
    selector_item = items[0]
    step = selector_item.range_items[0]

    # Fetch number of experiements
    no_experiments = len(qobj["experiments"])
    # Set up a labber sweep over the experiments (or no sweep, if only 1)
    if no_experiments > 1:
        step.range_type = "Start - Stop"
        step.start = 1
        step.stop = no_experiments
        # Stepsize, should always be 1.
        step.step = 1
    else:
        step.range_type = "Single"
        step.single = 1

    instr.values["QObj JSON"] = json.dumps(qobj)
    instr.values["QObj ID"] = qobj["qobj_id"]

    # set metadata
    s.log_name = "Test qobj"
    s.comment = "Comment for log"
    s.tags.project = "My project"
    s.tags.user = "Chalmers default user"
    s.tags.tags = ["Qobj"]

    # set timing info
    s.wait_between = 0.2

    return s


def update_step_single_value(scenario, name, value):
    scenario.get_step(name).range_items[0].single = value


def translate_parameter_name(id_, calibration_parameter):
    parameters = {
        "qubit_frequency": "Qubit {id} Frequency",
        "pi_amplitude": "Qubit {id} Amp",
        "drag_coefficient": "Qubit {id} Alpha",
        "readout_frequency": "Resonator {id} Frequency",
        "readout_amplitude": "pulses - Readout amplitude #{id}",
    }

    return parameters[calibration_parameter].format(id=str(id_))


def update_calibration_data(scenario, calibration):
    for qubit in calibration["qubits"]:
        id_ = qubit["id"]
        for parameter in qubit:
            if parameter == "id":
                continue
            elif parameter in [
                "readout_0_real_voltage",
                "readout_0_imag_voltage",
                "readout_1_real_voltage",
                "readout_1_imag_voltage",
            ]:
                state = scenario.get_instrument("State Discriminator")
                if parameter == "readout_0_real_voltage":
                    tmp = state.values["Pointer, QB{}-S0".format(id_)]
                    state.values["Pointer, QB{}-S0".format(id_)] = qubit[
                        parameter
                    ] + 1j * np.imag(tmp)
                elif parameter == "readout_0_imag_voltage":
                    tmp = state.values["Pointer, QB{}-S0".format(id_)]
                    state.values["Pointer, QB{}-S0".format(id_)] = (
                        np.real(tmp) + 1j * qubit[parameter]
                    )
                elif parameter == "readout_1_real_voltage":
                    tmp = state.values["Pointer, QB{}-S1".format(id_)]
                    state.values["Pointer, QB{}-S1".format(id_)] = qubit[
                        parameter
                    ] + 1j * np.imag(tmp)
                elif parameter == "readout_1_imag_voltage":
                    tmp = state.values["Pointer, QB{}-S1".format(id_)]
                    state.values["Pointer, QB{}-S1".format(id_)] = (
                        np.real(tmp) + 1j * qubit[parameter]
                    )
            else:
                update_step_single_value(
                    scenario, translate_parameter_name(id_, parameter), qubit[parameter]
                )


def add_waveforms(scenario, n_qubits):
    channel = "pulses - Trace - {waveform}{id}"
    # Add waveforms for qubit XY and Z lines
    for i in range(n_qubits):
        for j in ["I", "Q", "Z"]:
            scenario.add_log(channel.format(waveform=j, id=str(i + 1)))

    # Add readout waveforms
    scenario.add_log("pulses - Trace - Readout I")
    scenario.add_log("pulses - Trace - Readout Q")


def add_readout_voltages(scenario, n_qubits):
    channel = "QA - Result {id}"
    for i in range(n_qubits):
        scenario.add_log(channel.format(id=str(i + 1)))
