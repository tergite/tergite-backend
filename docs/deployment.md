# Deployment

How to install and run BCC on a machine that serves a device. To run it from a checkout
while developing, follow the [README's quick start](../README.md#quick-start) instead.

## Install as a service

Each release ships a deb (Linux amd64) and a macOS installer (arm64). Both run devices on
Qblox clusters and simulated devices.

1. Install the package. The deb asks for a first instance: its name (which becomes its
   `DEFAULT_PREFIX`), its device, port and MSS URL.

   ```shell
   sudo apt install ./tergite-backend_<version>_amd64.deb
   ```

2. Create any other instance (or the first one on macOS, whose installer asks nothing).
   `--device` is `cluster` (the default), `sim_1q` (one simulated transmon) or `sim_2q` (two
   and their coupler). `--port` and `--mss-url` override the defaults.

   ```shell
   sudo bcc-provision lab1
   sudo bcc-provision sim1 --device sim_2q --port 8001
   ```

3. Configure the instance:
   - edit its configs in `/etc/bcc/<name>/`;
   - edit its starting calibration in `/var/lib/bcc/<name>/calibration.seed.toml`, which
     recalibrations rewrite;
   - copy MSS's `public-mss-key.pem` into `/etc/bcc/<name>/`, unless MSS runs on the same
     machine;
   - give MSS the `[[backends]]` entry and `public-bcc-key.pem` that provisioning printed.

4. Start it, now and on every boot:

   ```shell
   sudo systemctl enable --now bcc@lab1
   journalctl -u bcc@lab1 -f
   ```

| Path (Linux) | Holds |
| --- | --- |
| `/etc/bcc/<name>/` | `backend.env`, the key pair, the device configs; read-only to the service |
| `/var/lib/bcc/<name>/` | the calibration seed, the database, jobs and results |

On macOS the same folders are under `/usr/local/`, and a LaunchDaemon runs each instance.

## Run or install with uv

Without the packages, [uv](https://docs.astral.sh/uv/) can run `bcc` straight from the
repository, or install it as a [tool](https://docs.astral.sh/uv/guides/tools/). uv fetches the
Python it needs. This runs BCC in the foreground, not as a service.

```shell
# once, without installing
uvx --from git+https://github.com/tergite/tergite-backend@v<version> bcc start --env-file backend.env

# or installed on the PATH; `uv tool uninstall tergite-backend` removes it
uv tool install git+https://github.com/tergite/tergite-backend@v<version>
bcc start --env-file backend.env
```

Outside a checkout, the default paths of the other files do not exist, so the env file must
give each of them as an absolute path, as `bcc-provision` writes them: `BCC_STORAGE_ROOT`,
`BCC_BOOKING_DB_URL`, `BCC_PRIVATE_KEY_FILE`, `BCC_MSS_PUBLIC_KEY_PATH`,
`BCC_BACKEND_SETTINGS`, `BCC_CALIBRATION_SEED`, `BCC_QUANTIFY_CONFIG_FILE`,
`BCC_QUANTIFY_METADATA_FILE` and the three `BCC_CALIBRATION_*_CONFIG_FILE`s.

## The `bcc` command

| Command | Runs |
| --- | --- |
| `bcc start` (the default) | the API, a heavy worker per device and `--light-workers` light workers (default 1); stops them all if any stops |
| `bcc serve` | the API alone |
| `bcc worker --kind heavy` / `--kind light` | one worker alone |

`start` and `serve` take `--port`, `--host`, `--forwarded-allow-ips` and `--workers` (API
processes; `$WEB_CONCURRENCY` or 1).

`bcc` reads the env file named by `--env-file` or `BCC_ENV_FILE`, or else the `.env` in the
folder it starts in.

## Serve several devices from one process

Give each device's env file:

```shell
bcc start --env-file loke.env --env-file thor.env
# or
BCC_ENV_FILE="$PWD/loke.env:$PWD/thor.env" bcc start
```

- Each device's API is under its `DEFAULT_PREFIX`: `http://localhost:8000/loke/bookings`. A
  path naming no device reaches the first one.
- Each device gets its own heavy worker; the light workers serve every device.
- In the environment, `BCC_<NAME>` (say `BCC_MAX_IDLE_TIME`) overrides every device's env
  file, and `BCC_<PREFIX>_<NAME>` (say `BCC_LOKE_MAX_IDLE_TIME`) overrides one device's.

## Build the packages

The deb and the macOS installer install `build/bcc`: uv's build of Python, with the
dependencies `uv.lock` pins and the backend's wheel installed into it, and a `bcc` script
that runs it. It builds for the machine's own OS and architecture, in a minute or two.

```shell
pkg/build.sh
BCC_ENV_FILE="$PWD/.env" build/bcc/bcc start
```

The wheel that `uv tool install` installs runs the same `bcc` with no service around it:
`main.py` and `settings.py` are top-level modules, and the migrations a namespace package that
`utils/sql_db.py` points Alembic at. `tests/test_packaging.py` builds the wheel and runs it
outside the checkout.

CI builds both packages on every `v*` tag, installs each, smoke-tests a device on clusters
and a simulated one, and attaches them to the release.
