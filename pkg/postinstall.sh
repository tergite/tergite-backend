#!/bin/sh
# Gives the bcc account its directories, provisions the instance debconf asked for, and
# restarts the running instances after an upgrade. Never calls a package manager: dpkg holds
# its lock while this runs.

set -e

# deb's `configure` with no old version, or rpm's 1
case "$1:$2" in
	configure: | 1:) is_fresh=yes ;;
	*) is_fresh=no ;;
esac

# each instance's folder in /etc/bcc holds its secrets: root's, readable by bcc alone
mkdir -p /etc/bcc /var/lib/bcc
chown root:bcc /etc/bcc
chmod 0750 /etc/bcc
chown bcc:bcc /var/lib/bcc
chmod 0750 /var/lib/bcc

if [ "$1" = configure ] && [ -e /usr/share/debconf/confmodule ] &&
	{ [ "$is_fresh" = yes ] || [ "$DEBCONF_RECONFIGURE" = 1 ]; }; then
	. /usr/share/debconf/confmodule
	db_get @PACKAGE@/name
	name=$RET
	if [ -n "$name" ]; then
		db_get @PACKAGE@/executor
		executor=$RET
		db_get @PACKAGE@/port
		port=$RET
		db_get @PACKAGE@/mss_url
		mss_url=$RET
		db_get @PACKAGE@/redis
		redis_url=redis://localhost:6379/0
		if [ "$RET" = url ]; then
			db_get @PACKAGE@/redis_url
			redis_url=$RET
		fi
		db_get @PACKAGE@/redis_password
		# stdout is debconf's until db_stop
		REDIS_PASSWORD=$RET /usr/sbin/@BINARY@-provision "$name" --executor "$executor" \
			--port "$port" --mss-url "$mss_url" --redis-url "$redis_url" >&2
		# backend.env is the one place a password is kept
		db_set @PACKAGE@/redis_password ""
	fi
	db_stop
fi

if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	if [ "$is_fresh" = no ]; then
		systemctl try-restart --no-block '@BINARY@@*.service'
	fi
fi
