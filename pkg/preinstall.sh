#!/bin/sh
# Creates the bcc account both packages' instances run as, before the files land, so
# /etc/bcc can be its group's. It joins dialout, the SPI rack's serial device's group.

set -e

if ! id bcc >/dev/null 2>&1; then
	useradd --system --home-dir /var/lib/bcc --no-create-home \
		--shell "$(command -v nologin || echo /sbin/nologin)" bcc
fi

if getent group dialout >/dev/null 2>&1; then
	usermod -a -G dialout bcc
fi
