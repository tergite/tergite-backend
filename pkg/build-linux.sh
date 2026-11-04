#!/bin/sh
# Builds build/bcc for Linux in a Debian or Ubuntu container, installing curl and uv where
# missing: from the repo root, on Linux,
# `docker run --rm -e UV_PYTHON=3.13 -v "$PWD":/src -w /src debian:trixie pkg/build-linux.sh`.
# Not from a macOS checkout mounted so, as its disk folds case, which Python's terminfo files
# differ by. There is no musl build: PyQt5, which quantify needs, has no musl wheels.

set -e

if ! command -v curl >/dev/null 2>&1; then
	apt-get update -qq
	apt-get install -y -qq curl ca-certificates >/dev/null
fi
if ! command -v uv >/dev/null 2>&1; then
	curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

pkg/build.sh
