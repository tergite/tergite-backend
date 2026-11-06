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
"""Tests of gate-level jobs, whose circuits are OpenQASM 3, uploaded to POST /jobs

On the quantify executor, the dummy clusters acquire nothing meaningful, so each
transmon's acquisitions are made those of a state the test chooses.
"""

from pathlib import Path
from typing import Any, Dict, Generator, List

import numpy as np
import pytest
import toml
from pytest_lazy_fixtures import lf as lazy_fixture
from pytest_mock import MockerFixture
from sqlalchemy import create_engine

from libs.queues.dtos import Job, JobStatus
from tests.utils.circuits import get_circuit, get_circuit_job, get_gate_level_seed
from tests.utils.env import TEST_BOOKING_DB_URL
from tests.utils.jobs import (
    get_job_submission_metadata,
    get_stored_jobs,
    submit_jobs,
)
from tests.utils.params import (
    CLIENT_AND_WORKER_TUPLES,
    USERS,
)
from tests.utils.users import create_many_users

_SHOTS = 20
_SIMULATED_SHOTS = 100
_SIM_1Q_CLIENT_AND_WORKER = pytest.param(
    lazy_fixture("sim_1q_rest_client"),
    lazy_fixture("sim_1q_worker"),
    id="sim_1q",
)
_SIM_2Q_CLIENT_AND_WORKER = pytest.param(
    lazy_fixture("sim_2q_rest_client"),
    lazy_fixture("sim_2q_worker"),
    id="sim_2q",
)
_QUANTIFY_CLIENT_AND_WORKER = [
    pytest.param(
        lazy_fixture("quantify_rest_client"),
        lazy_fixture("quantify_worker"),
    )
]

# (circuits, transmons acquired in state 1, the memory expected)
_DISCRIMINATED_JOBS = [
    pytest.param(
        [get_circuit("x", "x $0;\nc[0] = measure $0;", num_clbits=1)],
        {"q12"},
        [["0x1"] * _SHOTS],
        id="x-on-qubit-0",
    ),
    pytest.param(
        [get_circuit("partial", "x $1;\nc[0] = measure $1;", num_clbits=2)],
        {"q13"},
        [["0x1"] * _SHOTS],
        id="qubit-1-alone-into-clbit-0",
    ),
    pytest.param(
        [
            get_circuit(
                "permuted",
                "x $0;\nc[1] = measure $0;\nc[0] = measure $1;",
                num_clbits=2,
            )
        ],
        {"q12"},
        [["0x2"] * _SHOTS],
        id="permuted-clbits",
    ),
    pytest.param(
        [
            get_circuit("first", "x $0;\nc[0] = measure $0;", num_clbits=1),
            get_circuit("second", "c[1] = measure $1;", num_clbits=2),
        ],
        {"q12"},
        [["0x1"] * _SHOTS, ["0x0"] * _SHOTS],
        id="two-circuits",
    ),
    pytest.param(
        [
            get_circuit(
                "bell",
                "h $0;\ncx $0, $1;\nc[0] = measure $0;\nc[1] = measure $1;",
                num_clbits=2,
            )
        ],
        {"q12", "q13"},
        [["0x3"] * _SHOTS],
        id="cz-on-a-coupler",
    ),
]


@pytest.fixture
def quantify_seed_file(tmp_path) -> Generator[str, Any, None]:
    """The test seed, its qubits' frequencies reachable on the dummy clusters and its
    first coupler calibrated"""
    seed = get_gate_level_seed()
    seed_file = tmp_path / "gate_level.seed.toml"
    with open(seed_file, "w") as file:
        toml.dump(seed, file)
    yield str(seed_file)


@pytest.mark.parametrize("circuits, excited, expected", _DISCRIMINATED_JOBS)
@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_quantify_discriminated_memory_is_in_clbit_order(
    client, worker, circuits, excited, expected, jobs_folder, mocker, fake_clock
):
    from tests.utils.executors.quantify import use_excited_transmons

    use_excited_transmons(mocker, excited=excited)
    job = get_circuit_job(circuits, shots=_SHOTS)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    assert got.status == JobStatus.SUCCESSFUL
    assert got.result.memory == expected


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_quantify_iq_memory_of_each_shot_is_in_clbit_order(
    client, worker, jobs_folder, mocker, fake_clock
):
    from tests.utils.executors.quantify import get_iq_point, use_excited_transmons

    use_excited_transmons(mocker, excited={"q12"})
    circuit = get_circuit(
        "iq", "x $0;\nc[1] = measure $0;\nc[0] = measure $1;", num_clbits=2
    )
    job = get_circuit_job([circuit], shots=_SHOTS, meas_level=1)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    clbit_points = [get_iq_point("q13", state=0), get_iq_point("q12", state=1)]
    assert got.status == JobStatus.SUCCESSFUL
    assert got.result.memory == [[_to_pairs(clbit_points)] * _SHOTS]


@pytest.mark.parametrize(
    "meas_level, meas_return", [(2, "single"), (1, "avg")], ids=["counts", "averaged"]
)
@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_quantify_result_says_how_the_job_ran(
    client, worker, meas_level, meas_return, jobs_folder, mocker, fake_clock
):
    """A client reads the memory back without the job's payload"""
    circuits = [
        get_circuit("first", "x $0;\nc[0] = measure $0;", num_clbits=1),
        get_circuit("second", "c[2] = measure $1;", num_clbits=3),
    ]
    job = get_circuit_job(
        circuits, shots=_SHOTS, meas_level=meas_level, meas_return=meas_return
    )

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    result = got.result
    assert (result.shots, result.meas_level, result.meas_return) == (
        _SHOTS,
        meas_level,
        meas_return,
    )
    assert [(c.name, c.num_clbits) for c in result.circuits] == [
        ("first", 1),
        ("second", 3),
    ]


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_quantify_averaged_iq_memory_has_one_point_per_clbit(
    client, worker, jobs_folder, mocker, fake_clock
):
    from tests.utils.executors.quantify import get_iq_point, use_excited_transmons

    use_excited_transmons(mocker, excited={"q12"})
    circuit = get_circuit(
        "avg", "x $0;\nc[1] = measure $0;\nc[0] = measure $1;", num_clbits=2
    )
    job = get_circuit_job([circuit], shots=_SHOTS, meas_level=1, meas_return="avg")

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    clbit_points = [get_iq_point("q13", state=0), get_iq_point("q12", state=1)]
    assert got.status == JobStatus.SUCCESSFUL
    assert got.result.memory == [[_to_pairs(clbit_points)]]


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_a_circuit_without_num_clbits_has_those_its_qasm_declares(
    client, worker, jobs_folder, mocker, fake_clock
):
    from tests.utils.executors.quantify import use_excited_transmons

    use_excited_transmons(mocker, excited={"q12"})
    circuit = get_circuit("x", "x $0;\nc[1] = measure $0;", num_clbits=2)
    del circuit["num_clbits"]
    job = get_circuit_job([circuit], shots=_SHOTS)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    assert got.status == JobStatus.SUCCESSFUL
    assert [(c.name, c.num_clbits) for c in got.result.circuits] == [("x", 2)]
    assert got.result.memory == [["0x2"] * _SHOTS]


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_circuit_that_measures_nothing_fails_its_job(
    client, worker, jobs_folder, mocker, fake_clock
):
    """A circuit without measurements has no results, which would leave the other
    circuits' results unread, so the job fails naming it"""
    circuits = [
        get_circuit("idle", "x $0;", num_clbits=0),
        get_circuit("measured", "x $0;\nc[0] = measure $0;", num_clbits=1),
    ]
    job = get_circuit_job(circuits, shots=_SHOTS)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    assert got.status == JobStatus.FAILED
    assert got.failure_reason == "circuits idle measure no qubit"


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_cz_of_uncoupled_qubits_fails_its_job(
    client, worker, jobs_folder, mocker, fake_clock
):
    circuit = get_circuit("cz", "cz $1, $2;\nc[0] = measure $1;", num_clbits=1)
    job = get_circuit_job([circuit], shots=_SHOTS)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    assert got.status == JobStatus.FAILED
    assert got.failure_reason == "qubits q13 and q14 have no coupler"


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_a_syntax_error_is_refused_at_upload_saying_where_it_is(
    client, worker, jobs_folder, mocker, fake_clock
):
    circuit = get_circuit("broken", "h $0\nx $0;\nc[0] = measure $0;", num_clbits=1)
    job = get_circuit_job([circuit], shots=_SHOTS)

    with client as client:
        response = _upload_job(client, job, mocker=mocker, jobs_folder=jobs_folder)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "message": "circuit broken is not valid OpenQASM 3: "
        "line 5, column 0: missing ';' at 'x'",
        "circuit_index": 0,
        "line": 5,
        "column": 0,
    }
    assert get_stored_jobs() == []


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
@pytest.mark.parametrize(
    "body, message",
    [
        (
            "rx $0;\nc[0] = measure $0;",
            "gate rx takes 1 parameter and 1 qubit, but got 0 parameters",
        ),
        (
            "cx $0;\nc[0] = measure $0;",
            "gate cx takes 0 parameters and 2 qubits, but got 1 qubit",
        ),
        (
            "U(0.1, 0.2) $0;\nc[0] = measure $0;",
            "gate U takes 3 parameters and 1 qubit, but got 2 parameters",
        ),
        ("c[0] = measure $0;\nif (c[0]) { x $0; }", "if statements are not supported"),
    ],
    ids=["parameter-missing", "qubit-missing", "too-few-parameters", "if-statement"],
)
def test_a_circuit_that_does_not_translate_is_refused_at_upload_with_the_reason(
    client, worker, jobs_folder, mocker, fake_clock, body, message
):
    circuits = [
        get_circuit("fine", "x $0;\nc[0] = measure $0;", num_clbits=1),
        get_circuit("bad", body, num_clbits=1),
    ]
    job = get_circuit_job(circuits, shots=_SHOTS)

    with client as client:
        response = _upload_job(client, job, mocker=mocker, jobs_folder=jobs_folder)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "message": message,
        "circuit_index": 1,
        "line": None,
        "column": None,
    }
    assert get_stored_jobs() == []


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_a_circuit_whose_num_clbits_differs_from_its_qasm_is_refused_at_upload(
    client, worker, jobs_folder, mocker, fake_clock
):
    circuit = get_circuit("x", "x $0;\nc[0] = measure $0;", num_clbits=1)
    circuit["num_clbits"] = 2
    job = get_circuit_job([circuit], shots=_SHOTS)

    with client as client:
        response = _upload_job(client, job, mocker=mocker, jobs_folder=jobs_folder)

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == (
        "circuit x declares 2 classical bits, but its OpenQASM has 1"
    )
    assert get_stored_jobs() == []


@pytest.mark.parametrize(
    "params",
    [
        {"circuits": [], "shots": 10},
        {"circuits": [get_circuit("x", "x $0;", 0)], "shots": 10, "meas_level": 0},
        {"circuits": [get_circuit("x", "x $0;", 0)], "shots": 0},
        {"circuits": [get_circuit("x", "x $0;", 0)], "shots": 10, "qobj": {}},
    ],
)
@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_invalid_circuit_job_is_refused_at_upload(
    client, worker, params, jobs_folder, mocker, fake_clock
):
    job = {"job_id": "a7a1ce1c-7c0f-4e3e-9b5a-0f2b8c1d2e3f", "params": params}

    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        [job_info] = get_job_submission_metadata(
            client, jobs=[job], users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Invalid file: ")


@pytest.mark.parametrize("client, worker", CLIENT_AND_WORKER_TUPLES)
def test_qobj_job_of_an_old_sdk_is_refused_at_upload(
    client, worker, jobs_folder, mocker, fake_clock
):
    qobj = {"qobj_id": "q", "header": {}, "config": {"shots": 10}, "experiments": []}
    job = {"job_id": "c3d1f0a2-5b6e-4f7a-8c9d-0e1f2a3b4c5d", "params": {"qobj": qobj}}

    with client as client:
        users = create_many_users(client, raw_users=USERS[:1])
        [job_info] = get_job_submission_metadata(
            client, jobs=[job], users=users, mocker=mocker, jobs_folder=jobs_folder
        )
        with open(job_info["file_path"], "rb") as file:
            response = client.post(
                "/jobs", files={"upload_file": file}, headers=job_info["headers"]
            )
        worker.run(is_burst=True)

    assert response.status_code == 400
    assert (
        "the backend takes OpenQASM 3 circuits only, not a qobj; "
        "upgrade the tergite SDK to send them"
    ) in response.json()["detail"]
    assert get_stored_jobs() == []


# (circuits, the share of each circuit's shots expected in each state, at least)
_SIMULATED_JOBS = [
    pytest.param(
        [get_circuit("x", "x $0;\nc[0] = measure $0;", num_clbits=1)],
        [{"0x1": 0.8}],
        id="x-on-qubit-0",
    ),
    pytest.param(
        [get_circuit("partial", "x $1;\nc[0] = measure $1;", num_clbits=2)],
        [{"0x1": 0.8}],
        id="qubit-1-alone-into-clbit-0",
    ),
    pytest.param(
        [
            get_circuit(
                "permuted",
                "x $0;\nc[1] = measure $0;\nc[0] = measure $1;",
                num_clbits=2,
            )
        ],
        [{"0x2": 0.8}],
        id="permuted-clbits",
    ),
    pytest.param(
        [
            get_circuit("first", "c[0] = measure $0;", num_clbits=1),
            get_circuit("second", "x $1;\nc[1] = measure $1;", num_clbits=2),
        ],
        [{"0x0": 0.8}, {"0x2": 0.8}],
        id="two-circuits",
    ),
    pytest.param(
        [
            get_circuit(
                "bell",
                "rz(-pi/2) $0;\nrx(-pi/2) $0;\nrz(-pi/2) $0;\nrz(-pi/2) $1;\n"
                "rx(-pi/2) $1;\nrz(-pi/2) $1;\ncz $0, $1;\nrz(-pi/2) $1;\n"
                "rx(-pi/2) $1;\nrz(-pi/2) $1;\nc[0] = measure $0;\nc[1] = measure $1;",
                num_clbits=2,
            )
        ],
        [{"0x0": 0.25, "0x3": 0.25}],
        id="bell",
    ),
]


# a 2-qubit simulation takes seconds, beside other workers more
@pytest.mark.timeout(600)
@pytest.mark.parametrize("circuits, expected_shares", _SIMULATED_JOBS)
@pytest.mark.parametrize("client, worker", [_SIM_2Q_CLIENT_AND_WORKER])
def test_simulated_memory_is_in_clbit_order(
    client, worker, circuits, expected_shares, jobs_folder, mocker, fake_clock
):
    job = get_circuit_job(circuits, shots=_SIMULATED_SHOTS)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    assert got.status == JobStatus.SUCCESSFUL
    assert [len(memory) for memory in got.result.memory] == [_SIMULATED_SHOTS] * len(
        circuits
    )
    for memory, shares in zip(got.result.memory, expected_shares):
        for state, share in shares.items():
            assert memory.count(state) >= share * _SIMULATED_SHOTS, memory


@pytest.mark.timeout(600)
@pytest.mark.parametrize("client, worker", [_SIM_1Q_CLIENT_AND_WORKER])
def test_one_qubit_simulator_runs_an_x(client, worker, jobs_folder, mocker, fake_clock):
    circuit = get_circuit("x", "x $0;\nc[0] = measure $0;", num_clbits=1)
    job = get_circuit_job([circuit], shots=_SIMULATED_SHOTS)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    assert got.status == JobStatus.SUCCESSFUL
    assert got.result.memory[0].count("0x1") >= 0.8 * _SIMULATED_SHOTS


@pytest.mark.timeout(600)
@pytest.mark.parametrize("client, worker", [_SIM_2Q_CLIENT_AND_WORKER])
def test_simulated_iq_memory_is_in_clbit_order(
    client, worker, jobs_folder, mocker, fake_clock
):
    from services.devices import get_device_calibration_info
    from services.scheduler.utils import apply_linear_discriminator

    circuit = get_circuit(
        "iq", "x $0;\nc[1] = measure $0;\nc[0] = measure $1;", num_clbits=2
    )
    job = get_circuit_job([circuit], shots=_SIMULATED_SHOTS, meas_level=1)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)
        calibration = get_device_calibration_info(
            create_engine(TEST_BOOKING_DB_URL), backend_name="sim_2q"
        )

    [memory] = got.result.memory
    points = np.array([[complex(*point) for point in shot] for shot in memory])
    first, second = sorted(int(qubit.id) for qubit in calibration.qubits)
    # clbit 0 holds qubit 1, left in 0; clbit 1 holds qubit 0, flipped to 1
    states = [
        apply_linear_discriminator(calibration, number, points[:, clbit])
        for clbit, number in enumerate([second, first])
    ]
    assert got.status == JobStatus.SUCCESSFUL
    assert points.shape == (_SIMULATED_SHOTS, 2)
    assert np.mean(states[0]) <= 0.2
    assert np.mean(states[1]) >= 0.8


@pytest.mark.parametrize(
    "meas_return, expected_shape",
    [("single", (_SHOTS, 2, 2)), ("avg", (1, 2, 2))],
)
@pytest.mark.parametrize("client, worker", [_SIM_2Q_CLIENT_AND_WORKER])
def test_simulator_iq_memory_has_a_point_per_measured_clbit(
    client, worker, meas_return, expected_shape, jobs_folder, mocker, fake_clock
):
    circuit = get_circuit("iq", "c[2] = measure $0;\nc[0] = measure $1;", num_clbits=3)
    job = get_circuit_job(
        [circuit], shots=_SHOTS, meas_level=1, meas_return=meas_return
    )

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    assert got.status == JobStatus.SUCCESSFUL
    assert np.shape(got.result.memory[0]) == expected_shape


@pytest.mark.parametrize("client, worker", [_SIM_2Q_CLIENT_AND_WORKER])
def test_simulator_refuses_a_gate_on_a_measured_qubit(
    client, worker, jobs_folder, mocker, fake_clock
):
    circuit = get_circuit("mid", "c[0] = measure $0;\nx $0;", num_clbits=1)
    job = get_circuit_job([circuit], shots=_SHOTS)

    with client as client:
        got = _run_job(client, worker, job, mocker=mocker, jobs_folder=jobs_folder)

    assert got.status == JobStatus.FAILED
    assert got.failure_reason == (
        "the simulated chip reads a transmon once between its resets, after what else "
        "plays on it"
    )


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_quantify_runs_a_gate_by_its_defcal(
    client, worker, jobs_folder, mocker, fake_clock
):
    from tests.utils.executors.quantify import use_excited_transmons

    use_excited_transmons(mocker, excited={"q12"})
    circuit = get_circuit(
        "defcal",
        'defcalgrammar "openpulse";\n'
        "defcal x $0 { play(q12_drive, gaussian(0.3, 40ns, 10ns)); }\n"
        "x $0;\nc[0] = measure $0;",
        num_clbits=1,
    )
    job = get_circuit_job([circuit], shots=_SHOTS)

    with client as client:
        got = _run_job(
            client,
            worker,
            job,
            mocker=mocker,
            jobs_folder=jobs_folder,
            is_pulse_allowed=True,
        )

    assert got.status == JobStatus.SUCCESSFUL
    assert got.result.memory == [["0x1"] * _SHOTS]


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_quantify_captures_the_iq_point_of_each_shot_of_a_cal_block(
    client, worker, jobs_folder, mocker, fake_clock
):
    circuit = get_circuit(
        "capture",
        'defcalgrammar "openpulse";\n'
        "cal {\n"
        "    play(q12_readout, constant(0.2, 1us));\n"
        "    c[0] = capture_v1(q12_readout, 800ns);\n"
        "}",
        num_clbits=1,
    )
    job = get_circuit_job([circuit], shots=_SHOTS, meas_level=1)

    with client as client:
        got = _run_job(
            client,
            worker,
            job,
            mocker=mocker,
            jobs_folder=jobs_folder,
            is_pulse_allowed=True,
        )

    assert got.status == JobStatus.SUCCESSFUL
    [shots] = got.result.memory
    assert len(shots) == _SHOTS
    assert all(len(points) == 1 and len(points[0]) == 2 for points in shots)


@pytest.mark.parametrize("client, worker", [_SIM_2Q_CLIENT_AND_WORKER])
def test_a_device_without_pulse_limits_refuses_a_pulse_level_job_at_upload(
    client, worker, jobs_folder, mocker, fake_clock
):
    """The simulators set no pulse limits, as they run no pulses, so they take no
    pulse-level job, even one its project may send"""
    job = get_circuit_job([_get_pulse_circuit(amplitude=0.1)], shots=_SHOTS)

    with client as client:
        response = _upload_job(
            client, job, mocker=mocker, jobs_folder=jobs_folder, is_pulse_allowed=True
        )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "the device takes no pulse-level jobs: its operators have set no pulse limits"
    )


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_a_pulse_level_job_its_project_may_not_send_is_refused_at_upload(
    client, worker, jobs_folder, mocker, fake_clock
):
    """MSS says on asking for a job's token whether its project may send pulse-level
    jobs; one it does not let through is refused, before it is stored"""
    job = get_circuit_job([_get_pulse_circuit(amplitude=0.1)], shots=_SHOTS)

    with client as client:
        response = _upload_job(client, job, mocker=mocker, jobs_folder=jobs_folder)

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "the job's project may not send pulse-level jobs"
    )
    assert get_stored_jobs() == []


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_a_pulse_level_job_beyond_the_limits_is_refused_at_upload(
    client, worker, jobs_folder, mocker, fake_clock
):
    job = get_circuit_job([_get_pulse_circuit(amplitude=0.9)], shots=_SHOTS)

    with client as client:
        response = _upload_job(
            client, job, mocker=mocker, jobs_folder=jobs_folder, is_pulse_allowed=True
        )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "the amplitude of a waveform on q12_drive, 0.9, is beyond the device's limit "
        "of 0.8"
    )


@pytest.mark.parametrize("client, worker", _QUANTIFY_CLIENT_AND_WORKER)
def test_a_pulse_level_job_is_checked_again_before_it_runs(
    client, worker, jobs_folder, mocker, fake_clock
):
    """The limits are checked where the job runs too, whatever way its file came: here,
    the frames' calibration the executor reads is gone after the upload"""
    job = get_circuit_job([_get_pulse_circuit(amplitude=0.1)], shots=_SHOTS)
    mocker.patch(
        "libs.quantum_executor.base.executor.get_calibrated_frame_frequencies",
        return_value={},
    )

    with client as client:
        got = _run_job(
            client,
            worker,
            job,
            mocker=mocker,
            jobs_folder=jobs_folder,
            is_pulse_allowed=True,
        )

    assert got.status == JobStatus.FAILED
    assert got.failure_reason == "frame q12_drive is not one of the device's"


def _run_job(
    client,
    worker,
    job: Dict[str, Any],
    mocker: MockerFixture,
    jobs_folder: Path,
    is_pulse_allowed: bool = False,
) -> Job:
    """Uploads the job as a user, its project letting it be pulse-level or not, and runs
    the worker until it is done

    Returns:
        the job as the device stores it
    """
    users = create_many_users(client, raw_users=USERS[:1])
    job_metadata = get_job_submission_metadata(
        client,
        jobs=[job],
        users=users,
        mocker=mocker,
        jobs_folder=jobs_folder,
        is_pulse_allowed=is_pulse_allowed,
    )
    submit_jobs(client, data=job_metadata)
    worker.run(is_burst=True)
    [stored_job] = get_stored_jobs()
    return stored_job


def _to_pairs(points: List[complex]) -> List[List[float]]:
    """Gets each IQ point as [re, im], as the memory holds it"""
    return [[point.real, point.imag] for point in points]


def _upload_job(
    client,
    job: Dict[str, Any],
    mocker: MockerFixture,
    jobs_folder: Path,
    is_pulse_allowed: bool = False,
):
    """Uploads the job as a user, its project letting it be pulse-level or not

    Returns:
        the response to the upload
    """
    users = create_many_users(client, raw_users=USERS[:1])
    [metadata] = get_job_submission_metadata(
        client,
        jobs=[job],
        users=users,
        mocker=mocker,
        jobs_folder=jobs_folder,
        is_pulse_allowed=is_pulse_allowed,
    )
    with open(metadata["file_path"], "rb") as file:
        return client.post(
            "/jobs", files={"upload_file": file}, headers=metadata["headers"]
        )


def _get_pulse_circuit(amplitude: float) -> Dict[str, Any]:
    """Gets a circuit that plays a pulse of the amplitude on q12's drive, then measures"""
    return get_circuit(
        "pulses",
        'defcalgrammar "openpulse";\n'
        f"cal {{ play(q12_drive, constant({amplitude}, 40ns)); }}\n"
        "c[0] = measure $0;",
        num_clbits=1,
    )
