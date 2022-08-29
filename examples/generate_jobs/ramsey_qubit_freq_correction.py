# This code is part of Tergite
#
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

import measurement_jobs.measurement_jobs as measurement_jobs


# INSTRUMENT = ZI

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

    job = measurement_jobs.mk_job_ramsey(
        # Mandatory parameters for measurement job
        readout_resonance_freq = 5.99931e9,  # depends on pulsed resonator spectroscopy result
        drive_freq = 274.68e6,  # depends on two_tone
        drive_amp = 48e-3,  # depends on rabi = result / 2
        num_pts = 191,

        # Optional arguments to override calibration supervisor defaults
        is_calibration_sup_job = False,
        # Optional arguments to override any other parameters from the
        # defaults TOML file in measurement_jobs/parameter_defaults/
        #
    )
    return job



"""
 Parameters (need updating)
 ---------
 "drive_amp": Control MQPG drive signal amplitude, used when "Single" or "Span" range type is used
 "num_pts": number of points of measurement
"""


if __name__ == "__main__":
    main()
