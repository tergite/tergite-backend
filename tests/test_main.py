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
import http.server
import multiprocessing
import signal
import socket
import time

import pytest

import main
from services.scheduler import WorkerDeadline


@pytest.fixture
def started_processes():
    """The processes a test starts, killed after it if alive, as some ignore SIGTERM, which
    multiprocessing would otherwise wait on as the tests end"""
    processes = []
    yield processes
    for process in processes:
        if process.is_alive():
            process.kill()
            process.join()


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
def test_a_worker_past_its_deadline_is_killed_and_stops_the_rest(started_processes):
    """A worker stuck where no signal handler runs is killed, as SIGTERM would not stop it"""
    stuck_deadline = WorkerDeadline()
    stuck = multiprocessing.Process(
        target=_get_stuck,
        args=(stuck_deadline,),
        name="heavy worker of Alpha",
    )
    idle = multiprocessing.Process(target=_wait_forever, name="light worker 1")
    started_processes.extend([stuck, idle])
    for process in (stuck, idle):
        process.start()

    stopped = main._wait_for_first_stop(
        [stuck, idle], deadlines={stuck: stuck_deadline, idle: WorkerDeadline()}
    )

    assert stopped is stuck
    assert stuck.exitcode == -signal.SIGKILL
    assert not idle.is_alive()


@pytest.mark.timeout(60)
def test_a_worker_stuck_before_its_first_pass_is_killed_at_its_start_up_deadline(
    started_processes,
):
    """As connecting to the clusters may hang, before the worker sets a deadline itself"""
    deadline = WorkerDeadline()
    starting = multiprocessing.Process(
        target=_wait_forever, name="heavy worker of Alpha"
    )

    started_processes.append(starting)
    main._start_processes({starting: deadline}, start_seconds=0.5)
    stopped = main._wait_for_first_stop([starting], deadlines={starting: deadline})

    assert stopped is starting
    assert starting.exitcode == -signal.SIGKILL


@pytest.mark.timeout(60)
def test_an_api_that_stops_answering_its_health_check_is_killed(
    monkeypatch, started_processes
):
    """As a hung event loop leaves the process alive, which nothing else would notice"""
    monkeypatch.setattr(main, "DEADLINE_CHECK_SECONDS", 0.2)
    port = _get_free_port()
    api = multiprocessing.Process(
        target=_answer_once_then_hang, args=(port,), name="api"
    )
    started_processes.append(api)
    api.start()

    stopped = main._wait_for_first_stop(
        [api],
        deadlines={},
        api_health=main.ApiHealth(
            api, url=f"http://127.0.0.1:{port}/health", timeout=0.2
        ),
    )

    assert stopped is api
    assert api.exitcode == -signal.SIGKILL


@pytest.mark.timeout(60)
def test_systemd_is_told_bcc_start_is_alive_while_every_child_is_on_time(
    monkeypatch, notify_socket, started_processes
):
    monkeypatch.setattr(main, "DEADLINE_CHECK_SECONDS", 0.2)
    deadline = WorkerDeadline()
    deadline.set_in(60)
    worker = multiprocessing.Process(target=_wait_then_stop, args=(1,), name="worker")
    started_processes.append(worker)
    worker.start()

    main._wait_for_first_stop([worker], deadlines={worker: deadline})

    assert notify_socket.recv(64) == b"WATCHDOG=1"


def test_a_lone_worker_under_a_watchdog_keeps_it_fed_within_its_start_up_deadline(
    notify_socket,
):
    deadline = main._start_own_watchdog(watchdog_seconds=0.2, start_seconds=0.5)

    time.sleep(1)
    fed = []
    while True:
        try:
            fed.append(notify_socket.recv(64))
        except TimeoutError:
            break

    assert deadline.get_seconds_left() < 0
    assert 3 <= len(fed) <= 8


def _wait_then_stop(seconds: float) -> None:
    time.sleep(seconds)


def _get_stuck(deadline: WorkerDeadline) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    deadline.set_in(0.5)
    while True:
        time.sleep(1)


def _wait_forever() -> None:
    while True:
        time.sleep(1)


def _get_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _answer_once_then_hang(port: int) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    class Handler(http.server.BaseHTTPRequestHandler):
        answered = False

        def do_GET(self) -> None:
            if Handler.answered:
                time.sleep(3600)
            Handler.answered = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')

    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
