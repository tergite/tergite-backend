#!/bin/sh
# Provisions an instance with an installed package and loads it, which imports every executor
# dependency and reads the instance's settings, needing no MSS: as root, from
# the repo root, `pkg/smoke-test.sh bcc` or `pkg/smoke-test.sh bcc.simulator`

set -e

binary=$1
if [ "$(uname -s)" = Darwin ]; then
	etc_dir=/usr/local/etc/bcc
	PATH="/usr/local/bin:/usr/local/sbin:$PATH"
else
	etc_dir=/etc/bcc
fi

"$binary-provision" smoke >/dev/null
cp tests/fixtures/mss_public_key.pem "$etc_dir/smoke/public-mss-key.pem"
BCC_ENV_FILE="$etc_dir/smoke/backend.env" "$binary" --help
