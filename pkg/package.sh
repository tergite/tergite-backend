#!/bin/sh
# Packages a built binary with nfpm: from the repo root, with VERSION and ARCH set,
# `pkg/package.sh bcc deb` or `pkg/package.sh bcc.simulator apk`.

set -e

binary=$1
format=$2
[ -n "$format" ] || {
	echo "usage: $0 bcc|bcc.simulator deb|rpm|apk" >&2
	exit 1
}

pkg/render.sh "$binary" >/dev/null
mkdir -p dist
docker run --rm -e VERSION -e ARCH -v "$PWD":/src -w /src goreleaser/nfpm:v2.48.0 \
	package -f "build/pkg-$binary/nfpm.yaml" -p "$format" -t dist/
