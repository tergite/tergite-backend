#!/bin/sh
# Gives the bcc account its directories and restarts the running instances after an upgrade.
# A fresh install has no instance yet: provisioning one creates and enables it.

set -e

# deb's `configure` with no old version, rpm's 1, or apk's post-install (its upgrades
# run post-upgrade with the old version too)
case "$1:$2" in
	configure: | 1: | [0-9]*.*:) is_fresh=yes ;;
	*) is_fresh=no ;;
esac

# each instance's folder in /etc/bcc holds its secrets: root's, readable by bcc alone
mkdir -p /etc/bcc /var/lib/bcc
chown root:bcc /etc/bcc
chmod 0750 /etc/bcc
chown bcc:bcc /var/lib/bcc
chmod 0750 /var/lib/bcc

if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	if [ "$is_fresh" = no ]; then
		systemctl try-restart --no-block '@BINARY@@*.service'
	fi
elif command -v rc-service >/dev/null 2>&1 && [ "$is_fresh" = no ]; then
	for service in /etc/init.d/@SERVICE@.*; do
		[ -e "$service" ] || continue
		rc-service "${service##*/}" --ifstarted restart || true
	done
fi
