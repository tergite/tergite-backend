#!/bin/sh
# Creates the bcc account both packages' instances run as, before the files land, so
# /etc/bcc can be its group's. It joins dialout, the SPI rack's serial device's group.

set -e

if ! id bcc >/dev/null 2>&1; then
	if command -v useradd >/dev/null 2>&1; then
		useradd --system --home-dir /var/lib/bcc --no-create-home \
			--shell "$(command -v nologin || echo /sbin/nologin)" bcc
	else
		# Alpine's busybox
		addgroup -S bcc 2>/dev/null || true
		adduser -S -D -H -h /var/lib/bcc -s /sbin/nologin -G bcc bcc
	fi
fi

if getent group dialout >/dev/null 2>&1; then
	if command -v usermod >/dev/null 2>&1; then
		usermod -a -G dialout bcc
	else
		addgroup bcc dialout 2>/dev/null || true
	fi
fi
