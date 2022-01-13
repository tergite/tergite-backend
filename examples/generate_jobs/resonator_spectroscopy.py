# This code is part of Tergite
#
# (C) Copyright Andreas Bengtsson, Miroslav Dobsicek 2020
# (C) Copyright Abdullah-Al Amin 2021
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.


import numpy as np
import json
from uuid import uuid4
import requests
import pathlib
from tempfile import gettempdir
import settings

# settings
BCC_MACHINE_ROOT_URL = settings.BCC_MACHINE_ROOT_URL

REST_API_MAP = {"jobs": "/jobs"}


def main():
    job = generate_job()

    temp_dir = gettempdir()
    file = pathlib.Path(temp_dir) / str(uuid4())
    with file.open("w") as dest:
        json.dump(job, dest)

    with file.open("r") as src:
        files = {"upload_file": src}
        url = str(BCC_MACHINE_ROOT_URL) + REST_API_MAP["jobs"]
        response = requests.post(url, files=files)

        if response:
            print("Job has been successfully sent")

    file.unlink()


def generate_job():

    job = {
        "job_id": str(uuid4()),
        "type": "script",
        "name": "resonator_spectroscopy",
        "params": {
            "f_start": 6.5e9,
            "f_stop": 7.5e9,
            "if_bw": 1e3,
            "num_pts": 10001,
            "power": -40,
            "num_ave": 20,
        },
    }
    return job


"""
    Parameters
    ----------
        f_start   : (float) start sweep frequency [Hz]
        f_stop    : (float) stop sweep frequency [Hz]
        if_bw   : (float) IF bandwidth setting [Hz]
        num_pts : (int) number of frequency points
        power   : (int) output power of VNA [dBm]
        num_ave : (int) number of averages"""


if __name__ == "__main__":
    main()
