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

# a folder the packages install whole, as a onefile binary would unpack itself on every start
# nuitka-project: --standalone

# nuitka-project: --include-package=fastapi
# nuitka-project: --include-package=uvicorn
# nuitka-project: --include-package=toml
# nuitka-project: --include-package=tabulate
# nuitka-project: --include-package=cryptography
# nuitka-project: --include-package=filelock
# nuitka-project: --include-package=yaml
# nuitka-project: --include-package=sqlmodel
# nuitka-project: --include-package=jwt
# nuitka-project: --include-package=pydantic
# nuitka-project: --include-package=alembic
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/alembic.ini=alembic.ini
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/migrations/env.py=migrations/env.py
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/migrations/versions/*.py=migrations/versions/
# uvicorn imports the app by name, which Nuitka cannot follow
# nuitka-project: --include-module=api

# nuitka-project-if: os.getenv("EXECUTOR") == "qiskit":
#   nuitka-project: --output-filename=bcc.simulator
#   nuitka-project: --include-package=qiskit
#   nuitka-project: --include-package=jax
#   nuitka-project: --include-package=qiskit_dynamics
#   nuitka-project: --include-package=xarray
#   nuitka-project: --include-package=sklearn
#   nuitka-project: --include-package=numpy
#   nuitka-project: --include-package=h5py
#   openqasm3 imports its parser by the version of ANTLR, which Nuitka cannot follow
#   nuitka-project: --include-package=openqasm3
#   nuitka-project: --include-package=openpulse

# nuitka-project-else:
#   nuitka-project: --output-filename=bcc
#   nuitka-project: --include-package=quantify_core
#   nuitka-project: --include-package=quantify_scheduler
#   data these read at import or run time, which Nuitka includes only when told; it leaves
#   .py files out of package data, so quantify's version files are named
#   nuitka-project: --include-package-data=qcodes
#   nuitka-project: --include-package-data=quantify_core
#   nuitka-project: --include-package-data=quantify_scheduler
#   nuitka-project: --include-data-files={MAIN_DIRECTORY}/.venv/lib/python*/site-packages/quantify_core/_static_version.py=quantify_core/_static_version.py
#   nuitka-project: --include-data-files={MAIN_DIRECTORY}/.venv/lib/python*/site-packages/quantify_scheduler/_static_version.py=quantify_scheduler/_static_version.py
#   nuitka-project: --include-package=qblox_instruments
#   nuitka-project: --include-package=tergite_tuner
#   nuitka-project: --include-package=networkx
#   nuitka-project: --include-package=numpy
#   nuitka-project: --include-package=h5py
#   openqasm3 imports its parser by the version of ANTLR, which Nuitka cannot follow
#   nuitka-project: --include-package=openqasm3
#   nuitka-project: --include-package=openpulse

import argparse
import math
import multiprocessing
import os
import signal
import sys
import threading
import urllib.request
from multiprocessing.connection import wait
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import uvicorn

if TYPE_CHECKING:
    from services.scheduler import WorkerDeadline

COMMANDS = ("start", "serve", "worker")
# how often bcc start checks that no worker is past its deadline, and that the API answers
DEADLINE_CHECK_SECONDS = 5.0
# how long the API has to answer each /health, and how many in a row it may miss
HEALTH_CHECK_TIMEOUT_SECONDS = 5.0
HEALTH_CHECK_MAX_MISSES = 3


def main() -> None:
    """Runs the bcc command the command line names, start if it names none"""
    parser = _get_argument_parser()
    args = parser.parse_args(_with_default_command(sys.argv[1:]))
    if args.command == "start" and args.light_workers < 1:
        parser.error("--light-workers must be at least 1")
    _load_devices(args.env_files)
    _run_command(args)


def _start(
    host: str,
    port: int,
    forwarded_allow_ips: str | None,
    light_workers: int,
    env_files: list[str],
    workers: int | None,
) -> None:
    """Runs the API and the workers as child processes, stopping them all when any stops,
    so that the service manager restarts the backend whole. The children start the
    platform's way: macOS cannot fork a process that has loaded its frameworks."""
    import settings

    worker_deadlines = _get_worker_processes(
        env_files, devices=settings.get_device_names(), light_workers=light_workers
    )
    api_process = multiprocessing.Process(
        target=_serve,
        args=(host, port, forwarded_allow_ips, env_files, False, workers),
        name="api",
    )
    # a light worker loads every device, so the longest any of them allows
    start_seconds = max(
        settings.get_device_settings(device).max_worker_start_time
        for device in settings.get_device_names()
    )
    _start_processes(worker_deadlines, start_seconds=start_seconds)
    api_process.start()
    processes = [*worker_deadlines, api_process]

    # after the children start, which keep the handlers uvicorn and the workers install
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    api_health = ApiHealth(
        api_process, url=f"http://{_get_loopback_host(host)}:{port}/health"
    )
    stopped = _wait_for_first_stop(
        processes, deadlines=worker_deadlines, api_health=api_health
    )
    sys.exit(
        f"bcc: the {stopped.name} process stopped with exit code {stopped.exitcode}"
    )


def _get_worker_processes(
    env_files: list[str], devices: list[str], light_workers: int
) -> dict[multiprocessing.Process, "WorkerDeadline"]:
    """Gets the processes, not yet started, of a heavy worker for each device and of the
    light workers, each with the deadline it sets itself"""
    from services.scheduler import WorkerDeadline

    deadlines = {}
    for device in devices:
        deadline = WorkerDeadline()
        process = multiprocessing.Process(
            target=_run_worker,
            args=("heavy", env_files, device, deadline),
            name=f"heavy worker of {device}",
        )
        deadlines[process] = deadline
    for number in range(1, light_workers + 1):
        deadline = WorkerDeadline()
        process = multiprocessing.Process(
            target=_run_worker,
            args=("light", env_files, None, deadline),
            name=f"light worker {number}",
        )
        deadlines[process] = deadline
    return deadlines


def _start_processes(
    deadlines: dict[multiprocessing.Process, "WorkerDeadline"], start_seconds: float
) -> None:
    """Starts the workers, each with the deadline of its start-up, by which its first pass is
    to set one itself; 0 seconds are no limit"""
    for process, deadline in deadlines.items():
        deadline.set_in(start_seconds if start_seconds > 0 else math.inf)
        process.start()


def _wait_for_first_stop(
    processes: list[multiprocessing.Process],
    deadlines: dict[multiprocessing.Process, "WorkerDeadline"],
    api_health: Optional["ApiHealth"] = None,
) -> multiprocessing.Process:
    """Waits for any of the processes to stop, or a worker to pass its deadline, or the API to
    stop answering its health check, which it kills, as one stuck where no signal handler runs
    would not stop otherwise; then stops the rest

    Returns:
        the first that stopped, or the one killed
    """
    from utils.systemd import notify_systemd

    try:
        while True:
            wait(
                [process.sentinel for process in processes],
                timeout=DEADLINE_CHECK_SECONDS,
            )
            for process in processes:
                if not process.is_alive():
                    return process
            for process, deadline in deadlines.items():
                seconds_past = -deadline.get_seconds_left()
                if seconds_past > 0:
                    print(
                        f"bcc: the {process.name} process is {seconds_past:.0f}s past"
                        " its deadline, so stuck; killing it",
                        flush=True,
                    )
                    process.kill()
                    process.join()
                    return process
            if api_health is not None and api_health.is_unresponsive():
                print(
                    f"bcc: the {api_health.process.name} process missed"
                    f" {HEALTH_CHECK_MAX_MISSES} health checks in a row, so stuck;"
                    " killing it",
                    flush=True,
                )
                api_health.process.kill()
                api_health.process.join()
                return api_health.process
            # every child on time, for a unit with WatchdogSec, which restarts a bcc start
            # that hangs itself
            notify_systemd("WATCHDOG=1")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.join()


def _run_command(args: argparse.Namespace) -> None:
    """Runs the command the arguments name, with the devices loaded"""
    import settings

    if args.command == "start":
        _start(
            args.host,
            args.port or settings.get_device_settings().port,
            args.forwarded_allow_ips,
            args.light_workers,
            args.env_files,
            args.workers,
        )
    elif args.command == "serve":
        _serve(
            args.host,
            args.port or settings.get_device_settings().port,
            args.forwarded_allow_ips,
            args.env_files,
            args.should_reload,
            args.workers,
        )
    else:
        _run_worker(args.kind, args.env_files, args.device)


def _get_argument_parser() -> argparse.ArgumentParser:
    """Gets the parser of the bcc command's arguments"""
    parser = argparse.ArgumentParser(
        description="Manage the Backend Control Computer (BCC); start is the default command."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser(
        "start",
        help="Serve the API, and run a heavy worker for each device and the light workers",
    )
    serve_parser = commands.add_parser("serve", help="Serve the API")
    worker_parser = commands.add_parser("worker", help="Run one worker")

    for command_parser in (start_parser, serve_parser, worker_parser):
        command_parser.add_argument(
            "--env-file",
            action="append",
            default=[],
            dest="env_files",
            help="The env file of a device to serve, given once for each device; "
            "default: the files BCC_ENV_FILE names, separated as in PATH, or .env.",
        )
    for command_parser in (start_parser, serve_parser):
        command_parser.add_argument(
            "--host", default="0.0.0.0", help="Bind socket to this host."
        )
        command_parser.add_argument(
            "--port",
            type=int,
            help="Bind socket to this port; default: the first device's PORT.",
        )
        command_parser.add_argument(
            "--forwarded-allow-ips",
            help="Allow forwarding of IPS only if done by the given IP.",
        )
        command_parser.add_argument(
            "--workers",
            type=int,
            help="Number of API worker processes; default: $WEB_CONCURRENCY, or 1.",
        )
    start_parser.add_argument(
        "--light-workers",
        type=int,
        default=1,
        help="The number of workers for every device's postprocessing.",
    )
    serve_parser.add_argument(
        "--reload",
        dest="should_reload",
        default=False,
        action="store_true",
        help="Enable reload (not recommended for production binaries).",
    )
    worker_parser.add_argument(
        "--kind",
        required=True,
        choices=("heavy", "light"),
        help="heavy runs a device's preprocessing, executions and recalibrations; "
        "light runs every device's postprocessing.",
    )
    worker_parser.add_argument(
        "--device", help="The device a heavy worker runs; default: the first."
    )
    return parser


def _serve(
    host: str,
    port: int,
    forwarded_allow_ips: str | None,
    env_files: list[str],
    should_reload: bool,
    workers: int | None,
) -> None:
    _load_devices(env_files)
    # by import string, as uvicorn reloads and runs workers only so
    uvicorn.run(
        "api:app",
        host=host,
        reload=should_reload,
        port=port,
        workers=workers,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
    )


def _run_worker(
    kind: str,
    env_files: list[str],
    device: str | None = None,
    deadline: "WorkerDeadline | None" = None,
) -> None:
    _load_devices(env_files)
    import settings
    from utils.systemd import get_watchdog_seconds

    watchdog_seconds = get_watchdog_seconds()
    # run alone, as a unit's main process, it is watched by systemd rather than bcc start
    if deadline is None and watchdog_seconds is not None:
        deadline = _start_own_watchdog(
            watchdog_seconds,
            start_seconds=settings.get_device_settings(device).max_worker_start_time,
        )
    from api.worker import preload_worker_globals
    from services.scheduler.dispatcher import Dispatcher
    from utils.logging import set_up_logging

    is_heavy = kind == "heavy"
    devices = (
        [device or settings.get_device_names()[0]]
        if is_heavy
        else settings.get_device_names()
    )
    set_up_logging(settings.get_device_settings().log_level)
    resources = {
        name: preload_worker_globals(
            settings.get_device_settings(name), should_load_executor=is_heavy
        )
        for name in devices
    }
    dispatcher = Dispatcher(
        resources,
        should_run_heavy=is_heavy,
        should_run_light=not is_heavy,
        deadline=deadline,
    )
    # as RQ's warm shutdown did, after the stage under way
    signal.signal(signal.SIGTERM, lambda *_: dispatcher.stop())
    # whatever the log level, as uvicorn announces its start
    print(f"bcc: the {kind} worker is ready", flush=True)
    dispatcher.run()


def _start_own_watchdog(
    watchdog_seconds: float, start_seconds: float
) -> "WorkerDeadline":
    """Starts a thread that tells systemd every half of its WatchdogSec that the worker is
    alive, for as long as it is on time, which its start-up's deadline bounds until its first
    pass sets one; 0 start seconds are no limit

    Returns:
        the deadline the worker is to keep
    """
    from services.scheduler import WorkerDeadline
    from utils.systemd import keep_watchdog_fed_while_on_time

    deadline = WorkerDeadline()
    deadline.set_in(start_seconds if start_seconds > 0 else math.inf)
    threading.Thread(
        target=keep_watchdog_fed_while_on_time,
        args=(lambda: deadline.get_seconds_left() > 0, watchdog_seconds / 2),
        name="watchdog",
        daemon=True,
    ).start()
    return deadline


def _load_devices(env_files: list[str]) -> None:
    """Serves the devices of the env files, if any were given, in this process and the
    API workers it starts"""
    if not env_files:
        return
    # importing settings loads BCC_ENV_FILE's devices, as each API worker does
    os.environ["BCC_ENV_FILE"] = os.pathsep.join(
        str(Path(env_file).resolve()) for env_file in env_files
    )
    import settings

    settings.load_devices([Path(env_file) for env_file in env_files])


def _with_default_command(arguments: list[str]) -> list[str]:
    """Gets the arguments with start before them when they name no command"""
    if {"-h", "--help", *COMMANDS}.intersection(arguments):
        return arguments
    return ["start", *arguments]


class ApiHealth:
    """Whether the API still answers its /health, which bcc start asks from outside it, as a
    hung event loop leaves its process alive"""

    def __init__(
        self,
        process: multiprocessing.Process,
        url: str,
        timeout: float = HEALTH_CHECK_TIMEOUT_SECONDS,
    ):
        self.process = process
        self._url = url
        self._timeout = timeout
        self._misses = 0
        # misses count only once it has answered, as starting a device takes a while
        self._has_answered = False

    def is_unresponsive(self) -> bool:
        """Asks /health once, and checks whether the API has missed too many in a row

        Returns:
            True once the API has answered and then missed HEALTH_CHECK_MAX_MISSES in a row
        """
        try:
            with urllib.request.urlopen(self._url, timeout=self._timeout) as response:
                is_answered = response.status == 200
        except OSError:
            is_answered = False
        if is_answered:
            self._has_answered = True
            self._misses = 0
        elif self._has_answered:
            self._misses += 1
        return self._misses >= HEALTH_CHECK_MAX_MISSES


def _get_loopback_host(host: str) -> str:
    """Gets the host to ask the API on, which listens on the loopback too when on any address"""
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    return f"[{host}]" if ":" in host else host


if __name__ == "__main__":
    main()
