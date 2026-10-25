#!/bin/sh
# Forgets the unit. /etc/bcc, /var/lib/bcc and the bcc account stay, even on purge: they hold
# the only copy of the instances' secrets and their bookings.

set -e

if [ -d /run/systemd/system ]; then
	systemctl daemon-reload || true
fi

if [ "$1" = purge ] && [ -e /usr/share/debconf/confmodule ]; then
	. /usr/share/debconf/confmodule
	db_purge
fi
