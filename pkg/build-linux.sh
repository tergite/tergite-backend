#!/bin/sh
# Builds bcc on a machine that is not Linux, in a container, as Nuitka cannot cross-compile;
# on Linux, run pkg/build.sh: from the repo root,
# `docker run --rm -e UV_PYTHON=3.13 -v "$PWD":/src -w /src ubuntu:24.04 pkg/build-linux.sh`.
# Its glibc 2.39, as the release's, is the oldest it runs on. There is no musl build: PyQt5,
# which quantify needs, has no musl wheels or sources.

set -e

is_missing() {
	for tool in "$@"; do
		command -v "$tool" >/dev/null 2>&1 || return 0
	done
	return 1
}

# apt's packages and uv only where missing, as tergite/tergite-nuitka has them
if is_missing gcc g++ patchelf ccache curl; then
	apt-get update -qq
	apt-get install -y -qq gcc g++ patchelf ccache curl ca-certificates >/dev/null
fi
if is_missing uv; then
	curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

pkg/build.sh
