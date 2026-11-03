#!/bin/sh
# Renders pkg/ for bcc into build/pkg-bcc/: `pkg/render.sh`, from the repo root. Replaces the
# @...@ placeholders, which the packaging tools cannot.

set -e

package=tergite-backend binary=bcc name=default port=8000

rendered=build/pkg-$binary
rm -rf "$rendered"
mkdir -p "$rendered/debian" "$rendered/macos"
for file in pkg/* pkg/debian/* pkg/macos/*; do
	[ -f "$file" ] || continue
	sed -e "s/@PACKAGE@/$package/g" -e "s/@BINARY@/$binary/g" \
		-e "s/@DEFAULT_NAME@/$name/g" -e "s/@DEFAULT_PORT@/$port/g" \
		"$file" >"$rendered/${file#pkg/}"
done
echo "$package"
