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
"""Compares a run of dev/perf/record.sh with the baseline; exits 1 if any measure is worse than allowed"""
import csv
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ALLOWED_WORSENING = 0.10
# a rise this small is noise however large it is relative to the baseline, which may be 0
NOISE_FLOORS = {"ms": 10, "MiB": 5, "keys": 50, "failures": 0}
MEMORY_UNITS_IN_MIB = {"B": 1 / 1024**2, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}


def main(baseline_path: Path, run_path: Path) -> int:
    baseline = read_measures(baseline_path)
    run = read_measures(run_path)
    worse = [
        name
        for name in sorted(baseline)
        if is_worse(name, baseline[name], run.get(name))
    ]
    print_table(baseline, run, worse)
    return 1 if worse else 0


def read_measures(path: Path) -> Dict[str, float]:
    measures = {}
    measures.update(read_latencies(path / "locust_stats.csv"))
    measures.update(read_stage_times(path / "stage-times.csv"))
    measures.update(read_process_peaks(path / "process-stats.csv"))
    measures.update(read_redis_peaks(path / "redis-stats.csv"))
    return measures


def is_worse(name: str, baseline: float, run: Optional[float]) -> bool:
    if run is None:
        return True
    rise = run - baseline
    unit = name.split(":")[0].split()[-1]
    return rise > NOISE_FLOORS[unit] and rise > baseline * ALLOWED_WORSENING


def print_table(
    baseline: Dict[str, float], run: Dict[str, float], worse: List[str]
) -> None:
    for name in sorted(baseline):
        value = run.get(name, float("nan"))
        change = (
            f"{(value - baseline[name]) / baseline[name]:+.0%}"
            if baseline[name]
            else ""
        )
        mark = "  WORSE" if name in worse else ""
        print(f"{name:<60} {baseline[name]:>12.1f} {value:>12.1f} {change:>6}{mark}")


def read_latencies(path: Path) -> Dict[str, float]:
    measures = {}
    for row in _read_csv(path):
        route = f"{row['Type']} {row['Name']}".strip()
        measures[f"p95 ms: {route}"] = float(row["95%"])
        measures[f"failures: {route}"] = float(row["Failure Count"])
    return measures


def read_stage_times(path: Path) -> Dict[str, float]:
    """Reads each stage's 95th percentile time queued and running, and the jobs that failed"""
    times = defaultdict(list)
    failed_job_ids = set()
    for row in _read_csv(path):
        if row["status"] != "successful":
            failed_job_ids.add(row["job_id"])
        if row["queued_ms"]:
            times[f"p95 ms: {row['stage']} queued"].append(float(row["queued_ms"]))
        if row["running_ms"]:
            times[f"p95 ms: {row['stage']} running"].append(float(row["running_ms"]))
    measures = {name: _get_95th_percentile(values) for name, values in times.items()}
    measures["failures: jobs"] = float(len(failed_job_ids))
    return measures


def read_process_peaks(path: Path) -> Dict[str, float]:
    peaks = defaultdict(float)
    for row in _read_csv(path):
        name = f"peak MiB: {row['process']}"
        peaks[name] = max(peaks[name], _parse_quantity(row["memory"]))
    return dict(peaks)


def read_redis_peaks(path: Path) -> Dict[str, float]:
    peaks = {"peak MiB: redis used memory": 0.0, "peak keys: redis": 0.0}
    for row in _read_csv(path):
        peaks["peak MiB: redis used memory"] = max(
            peaks["peak MiB: redis used memory"], _parse_quantity(row["memory"])
        )
        peaks["peak keys: redis"] = max(peaks["peak keys: redis"], float(row["keys"]))
    return peaks


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open() as file:
        return list(csv.DictReader(file))


def _get_95th_percentile(values: List[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _parse_quantity(text: str) -> float:
    """'123.4MiB' is 123.4 MiB; a bare number is itself"""
    match = re.match(r"\s*([\d.]+)\s*([KMG]?i?B)?", text)
    return float(match.group(1)) * MEMORY_UNITS_IN_MIB.get(match.group(2) or "MiB", 1)


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
