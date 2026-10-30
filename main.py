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
#   nuitka-project: --include-package=redis
#   nuitka-project: --include-package=networkx
#   nuitka-project: --include-package=numpy
#   nuitka-project: --include-package=h5py

import argparse
import multiprocessing
import os
import signal
import sys
from multiprocessing.connection import wait
from pathlib import Path

import uvicorn

COMMANDS = ("start", "serve", "worker")


def main() -> None:
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

    processes = _get_worker_processes(
        env_files, devices=settings.get_device_names(), light_workers=light_workers
    )
    processes.append(
        multiprocessing.Process(
            target=_serve,
            args=(host, port, forwarded_allow_ips, env_files, False, workers),
            name="api",
        )
    )
    for process in processes:
        process.start()

    # after the children start, which keep the handlers uvicorn and the workers install
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    stopped = _wait_for_first_stop(processes)
    sys.exit(
        f"bcc: the {stopped.name} process stopped with exit code {stopped.exitcode}"
    )


def _get_worker_processes(
    env_files: list[str], devices: list[str], light_workers: int
) -> list[multiprocessing.Process]:
    """Gets the processes, not yet started, of a heavy worker for each device and of the
    light workers"""
    processes = [
        multiprocessing.Process(
            target=_run_worker,
            args=("heavy", env_files, device),
            name=f"heavy worker of {device}",
        )
        for device in devices
    ]
    processes.extend(
        multiprocessing.Process(
            target=_run_worker,
            args=("light", env_files),
            name=f"light worker {number}",
        )
        for number in range(1, light_workers + 1)
    )
    return processes


def _wait_for_first_stop(
    processes: list[multiprocessing.Process],
) -> multiprocessing.Process:
    """Waits for any of the processes to stop, then stops the rest

    Returns:
        the first that stopped
    """
    try:
        wait([process.sentinel for process in processes])
        return next(process for process in processes if not process.is_alive())
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


def _run_worker(kind: str, env_files: list[str], device: str | None = None) -> None:
    _load_devices(env_files)
    import settings
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
        resources, should_run_heavy=is_heavy, should_run_light=not is_heavy
    )
    # as RQ's warm shutdown did, after the stage under way
    signal.signal(signal.SIGTERM, lambda *_: dispatcher.stop())
    # whatever the log level, as uvicorn announces its start
    print(f"bcc: the {kind} worker is ready", flush=True)
    dispatcher.run()


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


if __name__ == "__main__":
    main()
