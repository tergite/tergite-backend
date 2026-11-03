#!/bin/sh
# Provisions an instance of a device on clusters and one of a simulated device with the
# installed package and loads each, which imports every executor dependency and reads the
# instance's settings, needing no MSS: as root, from the repo root, `pkg/smoke-test.sh`

set -e

if [ "$(uname -s)" = Darwin ]; then
	etc_dir=/usr/local/etc/bcc
	PATH="/usr/local/bin:/usr/local/sbin:$PATH"
else
	etc_dir=/etc/bcc
fi

for device in cluster sim_2q; do
	name=smoke_$device
	bcc-provision "$name" --device "$device" >/dev/null
	cp tests/fixtures/mss_public_key.pem "$etc_dir/$name/public-mss-key.pem"
	BCC_ENV_FILE="$etc_dir/$name/backend.env" bcc --help
done
