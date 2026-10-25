#!/bin/sh
# Creates a backend instance: `sudo @BINARY@-provision <name> [--executor E] [--port P]
# [--mss-url URL] [--redis-url URL]`, with a Redis password, if any, in REDIS_PASSWORD.
# <name> is its DEFAULT_PREFIX, which names /etc/bcc/<name>, /var/lib/bcc/<name> and the
# @BINARY@@<name> service, or on macOS /usr/local/etc/bcc/<name>, /usr/local/var/bcc/<name>
# and a LaunchDaemon. Writes backend.env, a key pair and the example configs, and leaves an
# existing instance alone.

set -e

if [ "$(uname -s)" = Darwin ]; then
	etc_dir=/usr/local/etc/bcc
	var_dir=/usr/local/var/bcc
	share_dir=/usr/local/share/@PACKAGE@
	mss_dir=/usr/local/etc/tergite-mss
	group=_bcc
else
	etc_dir=/etc/bcc
	var_dir=/var/lib/bcc
	share_dir=/usr/share/@PACKAGE@
	mss_dir=/etc/tergite-mss
	group=bcc
fi
examples=$share_dir/examples

main() {
	name=$1
	[ -n "$name" ] || fail "usage: @BINARY@-provision <name> [--executor E] [--port P] [--mss-url URL] [--redis-url URL]"
	shift
	case $name in
		*[!A-Za-z0-9_-]*) fail "the name '$name' may only have letters, digits, _ and -" ;;
	esac

	executor=@DEFAULT_EXECUTOR@
	port=@DEFAULT_PORT@
	mss_url=http://localhost:8002
	redis_url=redis://localhost:6379/0
	while [ $# -gt 0 ]; do
		case $1 in
			--executor) executor=$2 ;;
			--port) port=$2 ;;
			--mss-url) mss_url=$2 ;;
			--redis-url) redis_url=$2 ;;
			*) fail "unknown option $1" ;;
		esac
		shift 2
	done
	for value in "$port" "$mss_url" "$redis_url"; do
		case $value in
			*[\"\'\\\$\`\ ]*) fail "'$value' has a quote, backslash, dollar or space" ;;
		esac
	done

	dir=$etc_dir/$name
	data=$var_dir/$name
	# minimal Fedora has no hostname command
	host=$(hostname -f 2>/dev/null || uname -n)
	[ -f "$dir/backend.env" ] && {
		echo "@BINARY@: $dir/backend.env exists; leaving the instance '$name' alone"
		exit 0
	}

	umask 027
	mkdir -p "$dir" "$data/storage"
	chown "root:$group" "$dir"
	chown -R "$group:$group" "$data"
	copy_examples "$executor"
	# MSS knows the backend by its DEFAULT_PREFIX; the first name is [general_config]'s
	awk -v name="$name" '!is_named && /^name = / { print "name = \"" name "\""; is_named = 1; next } { print }' \
		"$dir/backend_config.toml" >"$dir/backend_config.toml.new"
	mv "$dir/backend_config.toml.new" "$dir/backend_config.toml"

	key_password=$(get_secret)
	# its progress dots kept quiet, as macOS's LibreSSL has no -quiet
	errors=$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -aes-256-cbc \
		-pass "pass:$key_password" -out "$dir/private-bcc-key.pem" 2>&1 >/dev/null) || fail "$errors"
	openssl rsa -pubout -passin "pass:$key_password" \
		-in "$dir/private-bcc-key.pem" -out "$dir/public-bcc-key.pem" 2>/dev/null
	# genpkey writes 0600 whatever the umask, which the service, in the group, could not read
	chmod 0640 "$dir/private-bcc-key.pem"
	# an MSS on this machine, as the tergite-mss package installs it
	if [ -f "$mss_dir/public-mss-key.pem" ]; then
		cp "$mss_dir/public-mss-key.pem" "$dir/public-mss-key.pem"
	fi

	# written last and whole: its existence is what says the instance is provisioned
	cat >"$dir/backend.env.new" <<-EOF
		# Written by @BINARY@-provision. Every other setting, and what it does, is in
		# $share_dir/dot-env-template.txt.
		DEFAULT_PREFIX=$name
		EXECUTOR_TYPE=$executor
		APP_SETTINGS=production
		BCC_PORT=$port
		BCC_MACHINE_ROOT_URL=http://$host:$port
		MSS_MACHINE_ROOT_URL=$mss_url
		RQ_REDIS_URL=$(add_password "$redis_url" "$REDIS_PASSWORD")

		STORAGE_ROOT=$data/storage
		BOOKING_DB_URL=sqlite:///$data/booking.db
		JWT_SECRET=$(get_secret)

		PRIVATE_KEY_FILE=$dir/private-bcc-key.pem
		PRIVATE_KEY_PASSWORD=$key_password
		MSS_PUBLIC_KEY_PATH=$dir/public-mss-key.pem

		BACKEND_SETTINGS=$dir/backend_config.toml
		CALIBRATION_SEED=$dir/calibration.seed.toml
		QUANTIFY_CONFIG_FILE=$dir/quantify-config.json
		QUANTIFY_METADATA_FILE=$dir/quantify-metadata.yml
		CALIBRATION_DEVICE_CONFIG_FILE=$dir/device_config.calib.toml
		CALIBRATION_NODE_CONFIG_FILE=$dir/node_config.calib.toml
		CALIBRATION_SPI_CONFIG_FILE=$dir/spi_config.calib.toml
	EOF
	chgrp -R "$group" "$dir"
	mv "$dir/backend.env.new" "$dir/backend.env"

	if [ "$(uname -s)" = Darwin ]; then
		plist=/Library/LaunchDaemons/se.chalmersnextlabs.@BINARY@.$name.plist
		sed "s/@NAME@/$name/g" "$share_dir/launchd.plist" >"$plist"
		chmod 0644 "$plist"
		start="launchctl bootstrap system $plist"
	else
		start="systemctl enable --now @BINARY@@$name"
	fi

	cat <<-EOF
		@BINARY@: provisioned '$name' in $dir.
		- Give MSS this backend: copy $dir/public-bcc-key.pem to the MSS machine and add
		  [[backends]]
		  name = "$name"
		  url = "http://$host:$port"
		  public_key_path = "<where you copied public-bcc-key.pem>"
		- Copy MSS's public-mss-key.pem to $dir/public-mss-key.pem, unless it is there already.
		- Edit the configs in $dir for the device, then start it: $start
	EOF
}

copy_examples() {
	case $1 in
		quantify)
			install -m 0640 "$examples/backend_config.example.toml" "$dir/backend_config.toml"
			install -m 0640 "$examples/calibration.seed.example.toml" "$dir/calibration.seed.toml"
			install -m 0640 "$examples/quantify-config.example.json" "$dir/quantify-config.json"
			install -m 0640 "$examples/quantify-metadata.example.yml" "$dir/quantify-metadata.yml"
			install -m 0640 "$examples/calib_device_config.example.toml" "$dir/device_config.calib.toml"
			install -m 0640 "$examples/node_config.example.toml" "$dir/node_config.calib.toml"
			install -m 0640 "$examples/spi_config.example.toml" "$dir/spi_config.calib.toml"
			;;
		qiskit_pulse_1q)
			install -m 0640 "$examples/backend_config.simq1.toml" "$dir/backend_config.toml"
			install -m 0640 "$examples/qiskit_pulse_1q.seed.toml" "$dir/calibration.seed.toml"
			;;
		qiskit_pulse_2q)
			install -m 0640 "$examples/backend_config.simq2.toml" "$dir/backend_config.toml"
			install -m 0640 "$examples/qiskit_pulse_2q.seed.toml" "$dir/calibration.seed.toml"
			;;
		*) fail "@BINARY@ runs no executor '$1'" ;;
	esac
}

add_password() {
	if [ -z "$2" ]; then
		printf '%s' "$1"
		return
	fi
	scheme=${1%%://*}
	rest=${1#*://}
	case $rest in
		*@*) url_user=${rest%%@*} url_host=${rest#*@} ;;
		*) url_user= url_host=$rest ;;
	esac
	printf '%s://%s:%s@%s' "$scheme" "$url_user" "$(encode_url "$2")" "$url_host"
}

encode_url() {
	printf '%s' "$1" | od -An -tx1 -v | tr -d ' \n' | sed 's/../%&/g'
}

get_secret() {
	od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

fail() {
	echo "@BINARY@: $1" >&2
	exit 1
}

main "$@"
