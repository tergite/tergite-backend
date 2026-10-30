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
"""Tests for the bcc command's arguments and the processes bcc start runs"""
import multiprocessing
import signal
import time

import pytest

import main
from services.scheduler import WorkerDeadline


def test_worker_takes_its_kind_and_each_env_file():
    args = main._get_argument_parser().parse_args(
        ["worker", "--kind", "light", "--env-file", "a.env", "--env-file", "b.env"]
    )

    assert (args.command, args.kind, args.env_files, args.device) == (
        "worker",
        "light",
        ["a.env", "b.env"],
        None,
    )


def test_start_runs_one_light_worker_unless_told():
    args = main._get_argument_parser().parse_args(["start"])

    assert (args.light_workers, args.port, args.workers) == (1, None, None)


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (["--port", "8000"], ["start", "--port", "8000"]),
        (["serve", "--port", "8000"], ["serve", "--port", "8000"]),
        (["--help"], ["--help"]),
    ],
)
def test_start_is_the_command_where_none_is_named(arguments, expected):
    assert main._with_default_command(arguments) == expected


def test_each_device_gets_a_heavy_worker_and_shares_the_light_ones():
    processes = main._get_worker_processes(
        ["a.env"], devices=["Alpha", "Beta"], light_workers=2
    )

    assert [process.name for process in processes] == [
        "heavy worker of Alpha",
        "heavy worker of Beta",
        "light worker 1",
        "light worker 2",
    ]
    assert all(process.pid is None for process in processes)


@pytest.mark.timeout(60)
def test_a_worker_past_its_deadline_is_killed_and_stops_the_rest():
    """A worker stuck where no signal handler runs is killed, as SIGTERM would not stop it"""
    stuck_deadline = WorkerDeadline()
    stuck = multiprocessing.Process(
        target=_get_stuck, args=(stuck_deadline,), name="heavy worker of Alpha"
    )
    idle = multiprocessing.Process(target=_wait_forever, name="light worker 1")
    for process in (stuck, idle):
        process.start()

    stopped = main._wait_for_first_stop(
        [stuck, idle], deadlines={stuck: stuck_deadline, idle: WorkerDeadline()}
    )

    assert stopped is stuck
    assert stuck.exitcode == -signal.SIGKILL
    assert not idle.is_alive()


@pytest.mark.timeout(60)
def test_a_worker_stuck_before_its_first_pass_is_killed_at_its_start_up_deadline():
    """As connecting to the clusters may hang, before the worker sets a deadline itself"""
    deadline = WorkerDeadline()
    starting = multiprocessing.Process(
        target=_wait_forever, name="heavy worker of Alpha"
    )

    main._start_processes({starting: deadline}, start_seconds=0.5)
    stopped = main._wait_for_first_stop([starting], deadlines={starting: deadline})

    assert stopped is starting
    assert starting.exitcode == -signal.SIGKILL


def _get_stuck(deadline: WorkerDeadline) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    deadline.set_in(0.5)
    while True:
        time.sleep(1)


def _wait_forever() -> None:
    while True:
        time.sleep(1)
