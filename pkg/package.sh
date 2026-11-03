#!/bin/sh
# Packages the built binary with nfpm: from the repo root, with VERSION and ARCH set,
# `pkg/package.sh deb` or `pkg/package.sh rpm`.

set -e

format=$1
[ -n "$format" ] || {
	echo "usage: $0 deb|rpm" >&2
	exit 1
}

pkg/render.sh >/dev/null
mkdir -p dist
docker run --rm -e VERSION -e ARCH -v "$PWD":/src -w /src goreleaser/nfpm:v2.48.0 \
	package -f build/pkg-bcc/nfpm.yaml -p "$format" -t dist/
