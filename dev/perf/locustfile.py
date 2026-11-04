# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""The BCC load scenario of the performance baselines; dev/perf/record.sh runs it"""

import base64
import json
import os
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from locust import HttpUser, between, task

FIXTURES_PATH = Path(__file__).parents[2] / "tests" / "fixtures"
MSS_PRIVATE_KEY = serialization.load_pem_private_key(
    (FIXTURES_PATH / "mss_private_key.pem").read_bytes(), password=None
)
JOB = json.loads((FIXTURES_PATH / "jobs_to_upload_simulator_1q.json").read_text())[0]
# whose jobs and bookings dev/perf/seed.py stored
BACKLOG_USER_ID = os.environ["PERF_BACKLOG_USER_ID"]
# whose jobs dev/perf/stage_times.py times
SDK_USER_ID = os.environ["PERF_SDK_USER_ID"]


class MssUser(HttpUser):
    """Reads the backlog as MSS does, with signed headers"""

    wait_time = between(0.5, 1.5)

    @task(4)
    def list_jobs(self) -> None:
        self.client.get("/jobs", headers=get_mss_headers("GET", "/jobs"))

    @task(4)
    def list_bookings(self) -> None:
        self.client.get("/bookings", headers=get_mss_headers("GET", "/bookings"))

    @task(2)
    def greet(self) -> None:
        self.client.get("/", headers=get_mss_headers("GET", "/"))


class SdkUser(HttpUser):
    """Submits jobs as the SDK does, with the token MSS got for each"""

    # few enough that the heavy worker keeps up, so the stage times measure the stages
    fixed_count = 2
    wait_time = between(1, 2)

    @task
    def submit_job(self) -> None:
        job_id = f"{uuid.uuid4()}"
        response = self.client.post(
            "/token",
            json={"user_id": SDK_USER_ID, "job_id": job_id},
            headers=get_mss_headers("POST", "/token", user_id=SDK_USER_ID),
        )
        token = decrypt_access_token(response.json()["access_token"])
        job = dict(JOB)
        job["job_id"] = job_id
        job_file = json.dumps(job).encode()
        self.client.post(
            "/jobs",
            files={"upload_file": ("job.json", job_file, "application/json")},
            headers={"Authorization": f"Bearer {token}"},
        )


def get_mss_headers(method: str, path: str, user_id: str = BACKLOG_USER_ID) -> dict:
    """Gets the headers MSS signs a request to the BCC with"""
    nonce = f"{uuid.uuid4()}"
    timestamp = f"{time.time()}"
    message = f"{user_id}|{nonce}|{timestamp}||{method}|{path}"
    signature = MSS_PRIVATE_KEY.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256(),
    )
    return {
        "x-mss-user-id": user_id,
        "x-mss-request-id": nonce,
        "x-mss-timestamp": timestamp,
        "x-mss-signature": base64.b64encode(signature).decode(),
    }


def decrypt_access_token(access_token: str) -> str:
    """Decrypts the job token the BCC encrypted for MSS, as MSS does"""
    token_bytes = MSS_PRIVATE_KEY.decrypt(
        base64.b64decode(access_token),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return token_bytes.decode()
