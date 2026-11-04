#!/bin/sh
# Provisions an instance of a device on clusters and one of a simulated device with the
# installed package, and runs bcc for each, then starts its heavy worker for a minute, which
# imports every executor dependency, failing if it stops or logs a traceback; the device on
# clusters, whose configs are the examples, runs on their dummy clusters. It needs no MSS: as
# root, from the repo root, `pkg/smoke-test.sh`

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
	log=$(mktemp)
	BCC_ENV_FILE="$etc_dir/$name/backend.env" bcc worker --kind heavy >"$log" 2>&1 &
	worker=$!
	sleep 60
	# a worker that cannot import, read or connect to what it needs stops, whatever it raised
	if ! kill "$worker" 2>/dev/null; then
		cat "$log" >&2
		echo "the heavy worker of $name stopped within a minute" >&2
		exit 1
	fi
	wait "$worker" 2>/dev/null || true
	if grep -q Traceback "$log"; then
		cat "$log" >&2
		echo "the heavy worker of $name logged a traceback" >&2
		exit 1
	fi
done
