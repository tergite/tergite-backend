# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright Abdullah-Al Amin 2021
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.


from pathlib import Path
from uuid import uuid4
import json
from scenario_scripts import (
    demodulation_scenario,
    qobj_scenario,
    qobj_dummy_scenario,
    resonator_spectroscopy_scenario,
)
import requests
import settings

from job_supervisor import inform_location, inform_failure, Location

# settings
STORAGE_ROOT = settings.STORAGE_ROOT
LABBER_MACHINE_ROOT_URL = settings.LABBER_MACHINE_ROOT_URL
BCC_MACHINE_ROOT_URL = settings.BCC_MACHINE_ROOT_URL
QUANTIFY_MACHINE_ROOT_URL = settings.QUANTIFY_MACHINE_ROOT_URL

REST_API_MAP = {"scenarios": "/scenarios", "qobj": "/qobj"}


def post_schedule_file(job_dict: dict, /):
    print(f"Received OpenPulse schedule")

    tmp_file = Path(STORAGE_ROOT) / (str(uuid4()) + ".to_quantify")

    with tmp_file.open("w") as store:
        json.dump(job_dict, store)  # copy incoming data to temporary file

    with tmp_file.open("r") as source:
        files = {
            "upload_file": (tmp_file.name, source),
            "send_logfile_to": (None, str(BCC_MACHINE_ROOT_URL)),
        }

        url = str(QUANTIFY_MACHINE_ROOT_URL) + REST_API_MAP["qobj"]
        print("Sending the pulse schedule to Quantify")
        response = requests.post(url, files=files)

    tmp_file.unlink()
    return response


def post_scenario_file(job_dict: dict, /):

    job_id = job_dict["job_id"]

    # Inform supervisor
    inform_location(job_id, Location.EXEC_W)

    print(f"Job script type: {job_dict['name']}")
    if job_dict["name"] == "demodulation_scenario":
        signal_array = job_dict["params"]["Sine - Frequency"]
        demod_array = job_dict["params"]["Demod - Modulation frequency"]

        scenario = demodulation_scenario(signal_array, demod_array)

        scenario.log_name = "Test signal demodulation - " + job_id
        # scenario.save("/tmp/my.json", save_as_json=True)

    elif job_dict["name"] == "qiskit_qasm_runner":
        scenario = qobj_scenario(job_dict)

        scenario.log_name += job_id

    elif job_dict["name"] == "qasm_dummy_job":
        scenario = qobj_dummy_scenario(job_dict)

        scenario.log_name += job_id

    elif job_dict["name"] == "resonator_spectroscopy":
        scenario = resonator_spectroscopy_scenario(job_dict)

        scenario.log_name += job_id

    else:
        raise NotImplementedError(f"Unknown script name {job_dict['name']}")

    # Store important information inside the scenario: using the tag list
    # 1) job_id
    # 2) script name
    # 3) if is_calibration_sup_job is True, set this field to True
    is_calibration_sup_job = job_dict.get("is_calibration_sup_job", False)
    scenario.tags.tags = [job_id, job_dict["name"]]
    if is_calibration_sup_job:
        scenario.tags.tags += [is_calibration_sup_job]

    scenario_file = Path(STORAGE_ROOT) / (job_id + ".labber")
    scenario.save(scenario_file)
    print(f"Scenario generated at {str(scenario_file)}")

    with scenario_file.open("rb") as source:
        files = {
            "upload_file": (scenario_file.name, source),
            "send_logfile_to": (None, str(BCC_MACHINE_ROOT_URL)),
        }
        url = str(LABBER_MACHINE_ROOT_URL) + REST_API_MAP["scenarios"]
        print("Sending the scenario to Labber")
        response = requests.post(url, files=files)

    scenario_file.unlink()
    return response


def job_execute(job_file: Path):
    print(f"Executing file {str(job_file)}")

    with job_file.open() as f:
        job_dict = json.load(f)

    job_id = job_dict["job_id"]

    # this is where the quantify redirect is
    if job_dict["name"] == "pulse_schedule":
        response = post_schedule_file(job_dict)
    else:
        try:
            response = post_scenario_file(job_dict)
        except NotImplementedError as err:
            print(err)
            print("Job failed")
            # Inform job supervisor about failure
            inform_failure(job_id, reason="unknown script name")
            return {"message": "failed"}

    if response:
        # clean up
        job_file.unlink()

        print("Job executed successfully")
        return {"message": "ok"}
    else:
        print("Job failed")
        # inform supervisor about failure
        inform_failure(job_id, reason="no response")
        return {"message": "failed"}
