# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import math
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from qiskit.qobj import PulseQobj
from quantify_scheduler.backends.graph_compilation import SerialCompiler
from quantify_scheduler.device_under_test.quantum_device import QuantumDevice

# SUT pieces
from app.libs.quantum_executor.quantify.experiment import QuantifyExperiment
from app.libs.quantum_executor.utils.config import load_quantify_config
from app.libs.quantum_executor.utils.portclock import generate_hardware_map
from app.tests.utils.fixtures import get_fixture_path, load_fixture


def _normalize_timing_table(df: pd.DataFrame) -> pd.DataFrame:
    """
    Produce a stable, comparable view of the timing table:
    - keep: port, clock, abs_time, duration, is_acquisition, op_type
    - round float columns to ns-scale granularity (1e-12 s)
    - op_type = type name of operation (e.g., 'GaussPulse', 'NumericalPulse', ...)
    - fill NaNs for strings with '' and for booleans with False
    """
    # Quantify’s timing_table can have these columns; tolerate missing ones.
    col_port = "port" if "port" in df.columns else None
    col_clock = "clock" if "clock" in df.columns else None
    col_abs = "abs_time" if "abs_time" in df.columns else "abs_time [s]"
    col_dur = "duration" if "duration" in df.columns else "duration [s]"
    col_acq = "is_acquisition" if "is_acquisition" in df.columns else None
    col_op = "operation" if "operation" in df.columns else None

    # Build minimal frame
    out = pd.DataFrame()
    if col_port:
        out["port"] = df[col_port].fillna("").astype(str)
    else:
        out["port"] = ""

    if col_clock:
        out["clock"] = df[col_clock].fillna("").astype(str)
    else:
        out["clock"] = ""

    def _round(x: float) -> float:
        try:
            return (
                0.0
                if (x is None or (isinstance(x, float) and math.isnan(x)))
                else round(float(x), 12)
            )
        except Exception:
            return float("nan")

    out["abs_time"] = df[col_abs].map(_round)
    out["duration"] = df[col_dur].map(_round)

    if col_acq:
        out["is_acquisition"] = df[col_acq].fillna(False).astype(bool)
    else:
        out["is_acquisition"] = False

    # op_type: prefer class name if object; else derive from string repr
    def _op_type(v):
        t = type(v).__name__
        if t not in ("str", "Series"):
            return t
        s = str(v)
        # e.g. "GaussPulse(...)" -> "GaussPulse"
        if "(" in s:
            return s.split("(", 1)[0]
        return s

    if col_op:
        out["op_type"] = df[col_op].map(_op_type)
    else:
        out["op_type"] = ""

    # Sort into a stable order
    out = out.sort_values(
        by=["abs_time", "clock", "port", "op_type", "duration"]
    ).reset_index(drop=True)
    return out


def _timing_table_df(compiled_schedule):
    """
    Return a pandas.DataFrame for the timing table across Quantify/pandas versions.
    """
    tt = getattr(compiled_schedule, "timing_table", None)
    if tt is None:
        raise RuntimeError("compiled schedule has no 'timing_table'")
    if callable(tt):
        tt = tt()
    # unwrap Styler or Quantify wrapper if present
    try:
        from pandas.io.formats.style import Styler  # type: ignore

        if isinstance(tt, Styler):
            return tt.data
    except Exception:
        pass
    if hasattr(tt, "data"):
        return tt.data
    return tt  # assume DataFrame


def _compile_schedule_from_qobj(qobj_dict: dict, quantify_config_path: Path):
    # 1) Load config into QuantumDevice
    qcfg = load_quantify_config(quantify_config_path)
    qdev = QuantumDevice("DUT")
    qdev.hardware_config(qcfg)
    compiler = SerialCompiler(name="compiler")
    comp_cfg = qdev.generate_compilation_config()

    # 2) Hardware map for 2 qubits (q0, q1) + coupler u0
    qubit_ids = ["q0", "q1"]
    coupling_dict = {"u0": ("q0", "q1")}
    hardware_map = generate_hardware_map(qubit_ids, coupling_dict, qcfg)

    # 3) Build PulseQobj from dict – use the INNER object if wrapped under params.qobj
    qdict = qobj_dict.get("params", {}).get("qobj", qobj_dict)
    qobj = PulseQobj.from_dict(qdict)

    # 4) Duck-typed native config (the mappers only need these attributes)
    native_cfg = SimpleNamespace(
        acq_return_type="append",
        protocol=SimpleNamespace(value="SSBIntegrationComplex"),
        bin_mode="append",
        meas_level=qobj.config.meas_level,
        meas_return=qobj.config.meas_return,
        meas_return_cols=getattr(qobj.config, "memory_slot_size", 1),
        n_qubits=qobj.config.n_qubits,
        shots=qobj.config.shots,
    )

    # 5) Translate to native
    expt = QuantifyExperiment.from_qobj_expt(
        name=getattr(qobj.experiments[0].header, "name", "exp"),
        expt=qobj.experiments[0],
        qobj_config=qobj.config,
        native_config=native_cfg,
        hardware_map=hardware_map,
    )

    # 6) Compile and normalize timing table
    compiled = compiler.compile(schedule=expt.schedule, config=comp_cfg)
    df = _timing_table_df(compiled)
    return compiled, _normalize_timing_table(pd.DataFrame(df))


def test_compiled_schedule_matches_fixture():
    """
    Compile the provided Qobj and verify the timing table matches the expected
    fixture (normalized comparison with rounded floats and op types).
    """

    fixture_path = Path(get_fixture_path())
    quantify_config_path = fixture_path / "two-qubit_quantify-config.json"
    expected_table_path = fixture_path / "compiled_schedule_expected.csv"
    qobj_dict = load_fixture("two-qubit_cz_qobj.json")

    if not expected_table_path.exists():
        pytest.skip(f"{expected_table_path} not found.")
    # compile & normalize actual timing table
    _, actual_df = _compile_schedule_from_qobj(qobj_dict, quantify_config_path)

    # load expected (CSV) & normalize columns just like actual
    expected_df_raw = pd.read_csv(expected_table_path)
    expected_df = _normalize_timing_table(expected_df_raw)


    # Allow tiny numeric jitter (floats) with atol = 1e-12 s
    pd.testing.assert_frame_equal(
        actual_df.reset_index(drop=True),
        expected_df.reset_index(drop=True),
        check_dtype=False,
        atol=1e-12,
        rtol=0,
        check_exact=False,
        obj="compiled schedule timing table",
    )
