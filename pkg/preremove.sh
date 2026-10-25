#!/bin/sh
# Stops and disables every instance of this binary before it goes. Their config and data in
# /etc/bcc and /var/lib/bcc stay, as does the bcc account, which the other package may use.

set -e

# deb's `upgrade` and rpm's 1 are an upgrade, which postinstall restarts the instances after
case "$1" in
	upgrade | 1) exit 0 ;;
esac

if [ -d /run/systemd/system ]; then
	for link in /etc/systemd/system/*.wants/@BINARY@@*.service; do
		[ -e "$link" ] || continue
		systemctl disable "${link##*/}" || true
	done
	systemctl stop '@BINARY@@*.service' || true
elif command -v rc-service >/dev/null 2>&1; then
	for service in /etc/init.d/@SERVICE@.*; do
		[ -e "$service" ] || continue
		rc-service "${service##*/}" stop || true
		rc-update del "${service##*/}" default || true
		rm -f "$service"
	done
fi
