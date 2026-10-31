#!/bin/bash
# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Starts the backend from this checkout with `main.py start`, passing on its arguments: from the
# repo root, `./start_bcc.sh` or `./start_bcc.sh --light-workers 2`. It reads the env file
# BCC_ENV_FILE names, .env by default, which a SOPS file encrypted with a Hashicorp Vault key
# may be: that needs sops, VAULT_ADDR and VAULT_TOKEN.

set -e

cd "$(dirname "$0")"
# the RQ workers fork a process for every job, which macOS refuses once frameworks are loaded
[ "$(uname -s)" != "Darwin" ] || export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

env_file="${BCC_ENV_FILE:-.env}"
if [ -f "$env_file" ] && grep -q "sops_hc_vault__" "$env_file"; then
	command -v sops > /dev/null || { echo "sops is needed to decrypt $env_file" >&2; exit 1; }
	set -o allexport
	. <(sops decrypt "$env_file")
	set +o allexport
	# its values are now in the environment, and the encrypted file's sops_ keys would be refused
	export BCC_ENV_FILE=/dev/null
fi

exec python main.py start "$@"
