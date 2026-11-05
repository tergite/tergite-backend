#!/usr/bin/env bash
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
# Writes tests/fixtures/schedule_snapshots/pre_refactor.json: seeded random circuits, each
# with what the schedule the backend compiled for it before the refactor played. The
# circuits go through the SDK of then, which sent PulseQobjs, and the backend of then,
# each checked out as a git worktree in the work folder.
#
# Usage: dev/schedule_snapshots/generate.sh <sdk-repo> <work-folder> [seed] [count]

set -euo pipefail

BACKEND_COMMIT=e9f2dea1
SDK_TAG=v2026.06.0

sdk_repo=$(cd "$1" && pwd)
work=$(mkdir -p "$2" && cd "$2" && pwd)
seed=${3:-2026}
count=${4:-24}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
snapshots="$repo/tests/fixtures/schedule_snapshots"
backend="$work/backend"
sdk="$work/sdk"

[ -d "$backend" ] || git -C "$repo" worktree add --detach "$backend" "$BACKEND_COMMIT"
[ -d "$sdk" ] || git -C "$sdk_repo" worktree add --detach "$sdk" "$SDK_TAG"
(cd "$backend" && uv sync --extra quantify)
[ -d "$sdk/.venv" ] || uv venv -p 3.12 "$sdk/.venv"
uv pip install -p "$sdk/.venv/bin/python" -e "$sdk"

# the settings the backend of then refuses to import without
fixtures="$backend/app/tests/fixtures"
sed -e "s#host:port#localhost:8005#g" -e "s#^STORAGE_ROOT=.*#STORAGE_ROOT=$work#" \
  "$backend/dot-env-template.txt" > "$work/.env"
export ENV_FILE="$work/.env" PYTHONPATH="$backend" DEFAULT_PREFIX=system_test \
  EXECUTOR_TYPE=quantify CALIBRATION_SEED="$snapshots/device.seed.toml" \
  BACKEND_SETTINGS="$fixtures/backend_config.toml" \
  CALIBRATION_NODE_CONFIG_FILE="$backend/node_config.example.toml" \
  CALIBRATION_DEVICE_CONFIG_FILE="$backend/calib_device_config.example.toml" \
  CALIBRATION_SPI_CONFIG_FILE="$fixtures/dummy_spi_config.calib.toml" \
  MSS_PUBLIC_KEY_PATH="$fixtures/mss_public_key.pem" \
  PRIVATE_KEY_FILE="$fixtures/private-bcc-key.pem"

"$backend/.venv/bin/python" "$here/baseline_schedules.py" device-info \
  "$fixtures/backend_config.toml" "$snapshots/device.seed.toml" "$work/device_info.json"
"$sdk/.venv/bin/python" "$here/sdk_circuits.py" \
  "$work/device_info.json" "$work/circuits.json" "$seed" "$count"
"$backend/.venv/bin/python" "$here/baseline_schedules.py" schedules \
  "$work/circuits.json" "$fixtures/backend_config.toml" "$snapshots/device.seed.toml" \
  "$fixtures/dummy-quantify-config.json" \
  "$repo/libs/quantum_executor/quantify/simulated_chip/timeline.py" \
  "$repo/tests/utils/snapshots.py" "$snapshots/pre_refactor.json"
