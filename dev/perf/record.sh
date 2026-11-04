#!/bin/bash
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
#
# Records the performance measures into <out-dir>: Locust on a BCC from this working tree, one
# simulated 1-qubit device whose heavy worker stubs out the simulation (dev/perf/stub_worker.py),
# talking to a stub MSS (dev/perf/mss_stub.py), with a stored backlog (dev/perf/seed.py). It
# samples the CPU and memory of the API, the heavy and the light worker; once the jobs
# submitted have finished, it records each one's time queued and running in each stage
# (dev/perf/stage_times.py).
#
# Usage
# =====
#
#   ./dev/perf/record.sh dev/perf/baselines           # the baseline
#   ./dev/perf/record.sh /tmp/perf                    # a run to compare with it
#   python3 dev/perf/compare.py dev/perf/baselines /tmp/perf
#
# Needs uv and curl, and ports 8102 and 8006 free.
# Compare only runs made on the same machine: the numbers are this machine's.

set -e

LOCUST_VERSION="2.46.7"
LOAD_USERS=10
LOAD_DURATION="2m"
SAMPLE_INTERVAL_SECONDS=2
ROOT_PATH="$(cd "$(dirname "$0")/../.." && pwd)"
# the Python the backend is released with; uv would otherwise take the newest it can find or
# download, whose wheels may not exist yet
export UV_PYTHON="${UV_PYTHON:-3.13}"
FIXTURES_PATH="$ROOT_PATH/tests/fixtures"
BCC_PORT=8102
MSS_PORT=8006
export PERF_BACKLOG_USER_ID="perf-backlog-user" PERF_SDK_USER_ID="perf-sdk-user"

main() {
  [[ -n "$1" ]] || exit_with_err "usage: $0 <out-dir>"
  for tool in uv curl; do
    command -v "$tool" &> /dev/null || exit_with_err "$tool is not available"
  done
  mkdir -p "$1"
  OUT_PATH="$(cd "$1" && pwd)"

  start_stack
  sample_processes &
  process_sampler_pid=$!
  uvx --from "locust==$LOCUST_VERSION" --with cryptography \
    locust -f "$ROOT_PATH/dev/perf/locustfile.py" --headless --only-summary \
    --host "http://127.0.0.1:$BCC_PORT" -u "$LOAD_USERS" -r "$LOAD_USERS" -t "$LOAD_DURATION" \
    --csv "$OUT_PATH/locust"
  (cd "$ROOT_PATH" && "$WORK_PATH/venv/bin/python" -m dev.perf.stage_times "$OUT_PATH/stage-times.csv")
  stop_stack
}

# Starts the stub MSS, the API and the heavy and light workers in WORK_PATH, after
# seeding the backlog
start_stack() {
  WORK_PATH="$(mktemp -d)"
  trap stop_stack EXIT
  write_env_file

  UV_PROJECT_ENVIRONMENT="$WORK_PATH/venv" uv sync -q --frozen --no-dev --project "$ROOT_PATH"
  # read on import, so the stub worker's swap of the executor sees the device
  export BCC_ENV_FILE="$WORK_PATH/perf.env"
  (cd "$ROOT_PATH" && exec "$WORK_PATH/venv/bin/python" -m dev.perf.mss_stub --port "$MSS_PORT") \
    > "$WORK_PATH/mss.log" 2>&1 &
  mss_pid=$!
  (cd "$ROOT_PATH" && "$WORK_PATH/venv/bin/python" -m dev.perf.seed) > "$WORK_PATH/seed.log" 2>&1
  (cd "$ROOT_PATH" && exec "$WORK_PATH/venv/bin/python" main.py serve --host 127.0.0.1 --port "$BCC_PORT") \
    > "$WORK_PATH/api.log" 2>&1 &
  api_pid=$!
  (cd "$ROOT_PATH" && exec "$WORK_PATH/venv/bin/python" -m dev.perf.stub_worker worker --kind heavy) \
    > "$WORK_PATH/heavy.log" 2>&1 &
  heavy_pid=$!
  (cd "$ROOT_PATH" && exec "$WORK_PATH/venv/bin/python" main.py worker --kind light) \
    > "$WORK_PATH/light.log" 2>&1 &
  light_pid=$!

  until curl -sf "http://127.0.0.1:$BCC_PORT/docs" > /dev/null; do
    kill -0 "$api_pid" 2> /dev/null || exit_with_err "the API stopped; see $WORK_PATH/api.log"
    sleep 0.5
  done
  until grep -q "worker is ready" "$WORK_PATH/heavy.log" && grep -q "worker is ready" "$WORK_PATH/light.log"; do
    kill -0 "$heavy_pid" 2> /dev/null || exit_with_err "the heavy worker stopped; see $WORK_PATH/heavy.log"
    kill -0 "$light_pid" 2> /dev/null || exit_with_err "the light worker stopped; see $WORK_PATH/light.log"
    sleep 0.5
  done
}

write_env_file() {
  mkdir "$WORK_PATH/storage"
  # the executor may write its calibration back, so a copy
  cp "$FIXTURES_PATH/qiskit_pulse_1q.seed.toml" "$WORK_PATH/calibration.seed.toml"
  {
    echo "BCC_DEFAULT_PREFIX=perf"
    echo "BCC_EXECUTOR_TYPE=quantify"
    echo "BCC_APP_SETTINGS=production"
    echo "BCC_JWT_SECRET=$(openssl rand -hex 32)"
    echo "BCC_MACHINE_ROOT_URL=http://127.0.0.1:$BCC_PORT"
    echo "BCC_MSS_MACHINE_ROOT_URL=http://127.0.0.1:$MSS_PORT"
    echo "BCC_STORAGE_ROOT=$WORK_PATH/storage"
    echo "BCC_BOOKING_DB_URL=sqlite:///$WORK_PATH/bookings.db"
    echo "BCC_BACKEND_SETTINGS=$FIXTURES_PATH/backend_config.simq1.toml"
    echo "BCC_CALIBRATION_SEED=$WORK_PATH/calibration.seed.toml"
    echo "BCC_QUANTIFY_CONFIG_FILE=$FIXTURES_PATH/dummy-quantify-config.json"
    echo "BCC_QUANTIFY_METADATA_FILE=$FIXTURES_PATH/dummy-quantify-metadata.yml"
    echo "BCC_CALIBRATION_NODE_CONFIG_FILE=$FIXTURES_PATH/chip_node_config.toml"
    echo "BCC_CALIBRATION_DEVICE_CONFIG_FILE=$FIXTURES_PATH/chip_calib_device_config.toml"
    echo "BCC_CALIBRATION_SPI_CONFIG_FILE=$FIXTURES_PATH/dummy_spi_config.calib.toml"
    echo "BCC_PRIVATE_KEY_FILE=$FIXTURES_PATH/private-bcc-key.pem"
    echo "BCC_MSS_PUBLIC_KEY_PATH=$FIXTURES_PATH/mss_public_key.pem"
    # the run is shorter than a day, so the start-up recalibration is its only one
    echo "BCC_DEFAULT_RECALIBRATION_INTERVAL=86400"
  } > "$WORK_PATH/perf.env"
}

stop_stack() {
  kill $process_sampler_pid "$api_pid" "$heavy_pid" "$light_pid" "$mss_pid" 2> /dev/null
  wait 2> /dev/null
  rm -rf "$WORK_PATH"
  process_sampler_pid=""
  trap - EXIT
}

# Writes a row of each process's CPU and resident memory to process-stats.csv every
# SAMPLE_INTERVAL_SECONDS. A worker runs each job in a forked child, so a worker's row is its
# tree: the memory of it and its children, and the CPU of it and the children it has waited for
sample_processes() {
  uv run -q --no-project --with psutil python - "$OUT_PATH/process-stats.csv" "$SAMPLE_INTERVAL_SECONDS" \
    "api=$api_pid" "heavy_worker=$heavy_pid" "light_worker=$light_pid" <<'PYTHON'
import csv
import sys
import time

import psutil

path, interval, *named_pids = sys.argv[1:]
processes = {
    name: psutil.Process(int(pid))
    for name, pid in (named_pid.split("=") for named_pid in named_pids)
}


def get_cpu_seconds(process):
    times = process.cpu_times()
    return times.user + times.system + times.children_user + times.children_system


def get_memory_mib(process):
    tree = [process] + process.children(recursive=True)
    return sum(get_rss(member) for member in tree) / 2**20


def get_rss(process):
    # a work horse can exit between being listed and being read
    try:
        return process.memory_info().rss
    except psutil.NoSuchProcess:
        return 0


cpu_seconds = {name: get_cpu_seconds(process) for name, process in processes.items()}
with open(path, "w", newline="") as file:
    writer = csv.writer(file)
    writer.writerow(["time", "process", "cpu", "memory"])
    # until stop_stack stops the processes
    while all(process.is_running() for process in processes.values()):
        time.sleep(float(interval))
        now = int(time.time())
        try:
            for name, process in processes.items():
                total = get_cpu_seconds(process)
                cpu = (total - cpu_seconds[name]) / float(interval) * 100
                cpu_seconds[name] = total
                writer.writerow([now, name, f"{cpu:.2f}%", f"{get_memory_mib(process):.1f}MiB"])
        except psutil.NoSuchProcess:
            break
        file.flush()
PYTHON
}

exit_with_err() {
  echo "$(date +'%Y-%m-%d %H:%M:%S') - ERROR: $1" >&2
  exit 1
}

main "$@"
