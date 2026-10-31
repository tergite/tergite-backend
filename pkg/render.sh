#!/bin/sh
# Renders pkg/ for a binary into build/pkg-<binary>/: `pkg/render.sh bcc|bcc.simulator`, from the
# repo root. Replaces the @...@ placeholders, which the packaging tools cannot.

set -e

binary=$1
case $binary in
	bcc)
		package=tergite-backend name=default port=8000
		executors=quantify default_executor=quantify
		;;
	bcc.simulator)
		package=tergite-backend-simulator name=simulator port=8001
		executors="qiskit_pulse_1q, qiskit_pulse_2q" default_executor=qiskit_pulse_1q
		;;
	*)
		echo "usage: $0 bcc|bcc.simulator" >&2
		exit 1
		;;
esac

rendered=build/pkg-$binary
rm -rf "$rendered"
mkdir -p "$rendered/debian" "$rendered/macos"
for file in pkg/* pkg/debian/* pkg/macos/*; do
	[ -f "$file" ] || continue
	sed -e "s/@PACKAGE@/$package/g" -e "s/@BINARY@/$binary/g" \
		-e "s/@DEFAULT_NAME@/$name/g" -e "s/@DEFAULT_PORT@/$port/g" \
		-e "s/@EXECUTORS@/$executors/g" -e "s/@DEFAULT_EXECUTOR@/$default_executor/g" \
		"$file" >"$rendered/${file#pkg/}"
done
echo "$package"
