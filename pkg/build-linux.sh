#!/bin/sh
# Builds bcc, or bcc.simulator with EXECUTOR=qiskit, in a container, as Nuitka cannot
# cross-compile: from the repo root,
# `docker run --rm -e EXECUTOR -v "$PWD":/src -w /src debian:bookworm pkg/build-linux.sh`.
# Bookworm's glibc 2.36 is the oldest it runs on. There is no musl build: neither PyQt5,
# which quantify needs, nor jaxlib, which the simulator needs, has musl wheels or sources.

set -e

apt-get update -qq
apt-get install -y -qq gcc g++ patchelf ccache curl ca-certificates >/dev/null
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv sync --dev --extra "${EXECUTOR:-quantify}"
uv run nuitka --assume-yes-for-downloads main.py
