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
# nuitka-project: --include-package=yaml
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
import multiprocessing
import signal
import sys
from multiprocessing.connection import wait

import uvicorn

import settings
from app.api import app
from app.api.worker import PreloadedRqWorker
from app.services.scheduler.queues import get_queue_pool
from app.utils.redis import get_redis_connection

QUEUES = (
    "general",
    "preprocessing",
    "normal_execution",
    "booked_execution",
    "postprocessing",
    "recalibration",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the Backend Control Computer (BCC)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser(
        "start", help="Serve the API and run a worker for every queue"
    )
    serve_parser = commands.add_parser("serve", help="Serve the API")
    rq_parser = commands.add_parser("rq", help="Run the worker of one queue")

    for command_parser in (start_parser, serve_parser):
        command_parser.add_argument(
            "--host", default="0.0.0.0", help="Bind socket to this host."
        )
        command_parser.add_argument(
            "--port",
            type=int,
            default=settings.BCC_PORT,
            help="Bind socket to this port.",
        )
        command_parser.add_argument(
            "--forwarded-allow-ips",
            help="Allow forwarding of IPS only if done by the given IP.",
        )
    serve_parser.add_argument(
        "--reload",
        default=False,
        action="store_true",
        help="Enable reload (not recommended for production binaries).",
    )
    rq_parser.add_argument(
        "--name", required=True, choices=QUEUES, help="The name of the queue to run."
    )

    args = parser.parse_args()

    if args.command == "start":
        _start(args.host, args.port, args.forwarded_allow_ips)
    elif args.command == "serve":
        _serve(args.host, args.port, args.forwarded_allow_ips, args.reload)
    else:
        _run_rq_worker(args.name)


def _start(host: str, port: int, forwarded_allow_ips: str | None) -> None:
    """Runs the API and the workers as child processes, stopping them all when any stops,
    so that the service manager restarts the backend whole. The children start the
    platform's way: macOS cannot fork a process that has loaded its frameworks."""
    processes = [
        multiprocessing.Process(target=_run_rq_worker, args=(queue,), name=queue)
        for queue in QUEUES
    ]
    processes.append(
        multiprocessing.Process(
            target=_serve, args=(host, port, forwarded_allow_ips), name="api"
        )
    )
    for process in processes:
        process.start()

    # after the children start, which keep the handlers uvicorn and rq install
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        wait([process.sentinel for process in processes])
        stopped = next(process for process in processes if not process.is_alive())
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.join()

    sys.exit(
        f"bcc: the {stopped.name} process stopped with exit code {stopped.exitcode}"
    )


def _serve(
    host: str, port: int, forwarded_allow_ips: str | None, reload: bool = False
) -> None:
    uvicorn.run(
        app,
        host=host,
        reload=reload,
        port=port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
    )


def _run_rq_worker(queue: str) -> None:
    queue_pool = get_queue_pool()
    worker = PreloadedRqWorker(
        [queue_pool.queues[f"{settings.DEFAULT_PREFIX}_{queue}"]],
        connection=get_redis_connection(),
    )
    worker.work()


if __name__ == "__main__":
    main()
