# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from pathlib import Path
import json
import time
from scenario_scripts import demodulation_scenario, qobj_scenario
from uuid import uuid4
import requests
from starlette.config import Config

# .env configuration
config = Config(".env")
STORAGE_ROOT = config("STORAGE_ROOT", default="/tmp")
LABBER_MACHINE_ROOT_URL = config(
    "LABBER_MACHINE_ROOT_URL", default="http://mc2-p045.mc2.chalmers.se:5000"
)

REST_API_MAP = {"scenarios": "/scenarios"}


def job_execute(job_file: Path):
    print(f"Executing file {str(job_file)}")

    job_dict = {}
    scenario_id = uuid4()
    scenario_file = Path(STORAGE_ROOT) / (str(scenario_id) + ".labber")

    with job_file.open() as f:
        job_dict = json.load(f)

    if job_dict["name"] == "demodulation_scenario":
        signal_array = job_dict["params"]["Sine - Frequency"]
        demod_array = job_dict["params"]["Demod - Modulation frequency"]

        scenario = demodulation_scenario(signal_array, demod_array)

        scenario.log_name = "Test signal demodulation - " + str(scenario_id)
        # scenario.save("/tmp/my.json", save_as_json=True)
        scenario.save(scenario_file)
        print(f"Scenario generated at {str(scenario_file)}")
    elif job_dict["name"] == "qiskit_qasm_runner":
        scenario = qobj_scenario(job_dict["params"]["qobj"])
        scenario.log_name += str(scenario_id)
        scenario.save(scenario_file)
        print(f"Scenario generated at {str(scenario_file)}")
    else:
        print(f"Unknown script name {job_dict['name']}")

    with scenario_file.open("rb") as source:
        files = {"upload_file": source}
        url = LABBER_MACHINE_ROOT_URL + REST_API_MAP["scenarios"]
        response = requests.post(url, files=files)

    if response:
        # clean up
        job_file.unlink()
        scenario_file.unlink()

        print("Demodulation scenario job executed successfully")
        return {"message": "ok"}
    else:
        print("Failed")
        return {"message": "failed"}
