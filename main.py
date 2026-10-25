# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

# nuitka-project: --onefile

# nuitka-project: --include-package=fastapi
# nuitka-project: --include-package=uvicorn
# nuitka-project: --include-package=toml
# nuitka-project: --include-package=requests
# nuitka-project: --include-package=tabulate
# nuitka-project: --include-package=cryptography
# nuitka-project: --include-package=filelock
# nuitka-project: --include-package=pyyaml
# nuitka-project: --include-package=networkx
# nuitka-project: --include-package=hiredis
# nuitka-project: --include-package=redis
# nuitka-project: --include-package=sqlmodel
# nuitka-project: --include-package=rq
# nuitka-project: --include-package=jwt
# nuitka-project: --include-package=pydantic
# nuitka-project: --include-package=bcrypt

# nuitka-project-if: os.getenv("EXECUTOR") == "qiskit":
#   nuitka-project: --output-filename=bcc.simulator
#   nuitka-project: --include-package=qiskit
#   nuitka-project: --include-package=jax
#   nuitka-project: --include-package=qiskit_dynamics
#   nuitka-project: --include-package=qiskit_ibm_provider
#   nuitka-project: --include-package=xarray
#   nuitka-project: --include-package=sklearn
#   nuitka-project: --include-package=numpy
#   nuitka-project: --include-package=h5py

# nuitka-project-else:
#   nuitka-project: --output-filename=bcc
#   nuitka-project: --include-package=quantify_core
#   nuitka-project: --include-package=quantify_scheduler
#   nuitka-project: --include-package=qblox_instruments
#   nuitka-project: --include-package=tergite_tuner
#   nuitka-project: --include-package=numpy
#   nuitka-project: --include-package=h5py

import argparse
from enum import Enum

import uvicorn
from redis import Connection
from rq import Worker

import settings
from app.api import app
from app.services.scheduler.queues import QueuePool, get_queue_pool


class _QueueName(str, Enum):
    """The names of queues"""
    GENERAL = "general"
    PREPROCESSING = "preprocessing"
    NORMAL_EXECUTION = "normal_execution"
    BOOKED_EXECUTION = "booked_execution"
    POSTPROCESSING = "postprocessing"
    RECALIBRATION = "recalibration"

    def __str__(self):
        return self.value

    def with_prefix(self, prefix: str) -> str:
        """Gets the full name of the queue with the prefix

        Args:
            prefix: prefix to add to the queue name

        Returns:
            the full name of the queue
        """
        return f"{prefix}_{self.name}"

def _run_rq_worker(redis_url: str, name: str, queue_pool: QueuePool):
    """Start a blocking run of an RQ worker waiting on the queue of given name.

    Args:
        redis_url: The redis URL where the worker will be started.
        name: The name of the queue.
    """
    with Connection(redis_url) as connection:
        worker = Worker(connection=connection, queues=[queue_pool.queues[name]])
        worker.work()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Manage the Backend Control Computer (BCC)."
    )

    commands = parser.add_subparsers(dest="command", required=True)
    server_parser = commands.add_parser("serve", help="Serve the API")
    rq_parser = commands.add_parser("rq", help="Manage the RQ workers")

    server_parser.add_argument("--host", default="0.0.0.0", help="Bind socket to this host.")
    server_parser.add_argument(
        "--port", type=int, default=8002, help="Bind socket to this port."
    )
    server_parser.add_argument(
        "--forwarded-allow-ips",
        help="Allow forwarding of IPS only if done by the given IP.",
    )
    server_parser.add_argument(
        "--reload",
        default=False,
        action="store_true",
        help="Enable reload (not recommended for production binaries).",
    )

    rq_parser.add_argument("--name", type=_QueueName, required=True, choices=tuple(_QueueName), help="The name of the queue to run.")

    args = parser.parse_args()

    if args.command == "rq":
        queue_name = args.name.with_prefix(settings.DEFAULT_PREFIX)
        _run_rq_worker(settings.RQ_REDIS_URL, name=queue_name, queue_pool=get_queue_pool())
    else:
        uvicorn.run(
                app,
                host=args.host,
                reload=args.reload,
                port=args.port,
                workers=1,
                proxy_headers=True,
                forwarded_allow_ips=args.forwarded_allow_ips,
            )
