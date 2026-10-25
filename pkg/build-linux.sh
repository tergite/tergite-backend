#!/bin/sh
# Builds bcc, or bcc.simulator with EXECUTOR=qiskit, on a machine that is not Linux, in a
# container, as Nuitka cannot cross-compile; on Linux, run pkg/build.sh: from the repo root,
# `docker run --rm -e EXECUTOR -e UV_PYTHON=3.12 -v "$PWD":/src -w /src ubuntu:24.04 pkg/build-linux.sh`.
# Its glibc 2.39, as the release's, is the oldest it runs on. There is no musl build: neither PyQt5,
# which quantify needs, nor jaxlib, which the simulator needs, has musl wheels or sources.

set -e

apt-get update -qq
apt-get install -y -qq gcc g++ patchelf ccache curl ca-certificates >/dev/null
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

pkg/build.sh
