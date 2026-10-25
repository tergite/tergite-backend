#!/bin/sh
# Packages a built binary with nfpm: from the repo root, with VERSION and ARCH set,
# `pkg/package.sh bcc deb` or `pkg/package.sh bcc.simulator apk`. nfpm expands no variables
# in file paths, so pkg/ is first rendered for the binary into build/pkg-<binary>/.

set -e

binary=$1
format=$2
case $binary in
	bcc)
		package=tergite-backend service=bcc name=default port=8000
		executors=quantify default_executor=quantify
		;;
	bcc.simulator)
		package=tergite-backend-simulator service=bcc-simulator name=simulator port=8001
		executors="qiskit_pulse_1q, qiskit_pulse_2q" default_executor=qiskit_pulse_1q
		;;
	*)
		echo "usage: $0 bcc|bcc.simulator deb|rpm|apk" >&2
		exit 1
		;;
esac

rendered=build/pkg-$binary
rm -rf "$rendered"
mkdir -p "$rendered/debian" dist
for file in pkg/* pkg/debian/*; do
	[ -f "$file" ] || continue
	sed -e "s/@PACKAGE@/$package/g" -e "s/@BINARY@/$binary/g" -e "s/@SERVICE@/$service/g" \
		-e "s/@DEFAULT_NAME@/$name/g" -e "s/@DEFAULT_PORT@/$port/g" \
		-e "s/@EXECUTORS@/$executors/g" -e "s/@DEFAULT_EXECUTOR@/$default_executor/g" \
		"$file" >"$rendered/${file#pkg/}"
done

docker run --rm -e VERSION -e ARCH -v "$PWD":/src -w /src goreleaser/nfpm:v2.48.0 \
	package -f "$rendered/nfpm.yaml" -p "$format" -t dist/
