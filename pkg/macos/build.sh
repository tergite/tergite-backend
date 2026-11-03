#!/bin/sh
# Packages the built binary as a macOS installer: from the repo root, with VERSION set,
# `pkg/macos/build.sh`. SIGN_IDENTITY, a "Developer ID Installer" certificate's name, signs it;
# without one Gatekeeper blocks it.

set -e

[ -n "$VERSION" ] || {
	echo "usage: VERSION=<version> $0" >&2
	exit 1
}

binary=bcc
package=$(pkg/render.sh)
rendered=build/pkg-$binary
output=dist/$package-$VERSION-macos-$(uname -m).pkg
root=$(mktemp -d)
scripts=$(mktemp -d)
trap 'rm -rf "$root" "$scripts"' EXIT

share=$root/usr/local/share/$package
install -d "$root/usr/local/bin" "$root/usr/local/sbin" "$root/usr/local/lib" "$share/examples"
# Nuitka's standalone build, whose executable finds the folder through the link
cp -R main.dist "$root/usr/local/lib/$package"
ln -s "../lib/$package/$binary" "$root/usr/local/bin/$binary"
install -m 0755 "$rendered/provision.sh" "$root/usr/local/sbin/$binary-provision"
install -m 0755 "$rendered/macos/uninstall.sh" "$share/"
install -m 0644 "$rendered/macos/launchd.plist" "$share/"
install -m 0644 .env.example "$share/env.example"
install -m 0644 backend_config.example.toml calibration.seed.example.toml \
	quantify-config.example.json quantify-metadata.example.yml calib_device_config.example.toml \
	node_config.example.toml spi_config.example.toml \
	tests/fixtures/backend_config.simq1.toml tests/fixtures/backend_config.simq2.toml \
	tests/fixtures/qiskit_pulse_1q.seed.toml tests/fixtures/qiskit_pulse_2q.seed.toml \
	"$share/examples/"
# extended attributes, like a download's quarantine, ship as ._ entries that Installer
# reapplies; com.apple.provenance cannot be removed and stays
xattr -cr "$root"
install -m 0755 "$rendered/macos/preinstall" "$rendered/macos/postinstall" "$scripts/"

mkdir -p dist
pkgbuild --root "$root" \
	--scripts "$scripts" \
	--identifier "se.chalmersnextlabs.$package" \
	--version "$VERSION" \
	--ownership recommended \
	${SIGN_IDENTITY:+--sign "$SIGN_IDENTITY"} \
	"$output"
echo "$output"
