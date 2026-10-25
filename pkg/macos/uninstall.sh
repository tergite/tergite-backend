#!/bin/sh
# macOS installers have no uninstall: `sudo /usr/local/share/@PACKAGE@/uninstall.sh`. Stops and
# removes this binary's instances' LaunchDaemons, and leaves /usr/local/etc/bcc,
# /usr/local/var/bcc and the _bcc account, which hold the instances' secrets and bookings.

set -e

[ "$(id -u)" = 0 ] || {
	echo "run it with sudo" >&2
	exit 1
}

for plist in /Library/LaunchDaemons/se.chalmersnextlabs.@BINARY@.*.plist; do
	[ -e "$plist" ] || continue
	launchctl bootout "system/$(basename "$plist" .plist)" 2>/dev/null || true
	rm -f "$plist"
done
rm -f /usr/local/bin/@BINARY@ /usr/local/sbin/@BINARY@-provision
rm -rf /usr/local/share/@PACKAGE@
pkgutil --forget se.chalmersnextlabs.@PACKAGE@ >/dev/null 2>&1 || true
echo "@PACKAGE@: removed; the instances' config and data are still in /usr/local/etc/bcc, /usr/local/var/bcc."
