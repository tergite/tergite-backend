# Contributing to tergite-backend

**This project is not accepting pull requests from the general public yet.**

**It is currently being developed by the core developers only.**

## Government Model

[Chalmers Next Labs AB (CNL)](https://chalmersnextlabs.se) manages and maintains this project on behalf of all contributors.

## Version Control

Tergite is developed on a separate version control system and mirrored publicly on GitHub.
If you are reading this on GitHub, then you are looking at a mirror. 

## Versioning

When versioning we follow the format `{year}.{month}.{patch_number}` e.g. `2023.12.0`.

## Contacting the Tergite Developers

Since the GitHub repositories are only mirrors, no GitHub pull requests or GitHub issue/bug reports 
are looked at. Please get in touch via email <quantum-nextlabs@chalmers.se> instead. 

Take note that the maintainers may not answer every email.

## But We Use [GitHub Flow](https://docs.github.com/en/get-started/quickstart/github-flow), So All Code Changes Happen Through Pull Requests

Pull requests are the best way to propose changes to the codebase (we
use [GitHub Flow](https://docs.github.com/en/get-started/quickstart/github-flow)). We actively welcome your pull
requests:

1. Clone the repo and create your branch from `main`.
2. If you've added code that should be tested, add tests.
3. If you've changed APIs, update the documentation.
4. Ensure the test suite passes.
5. Make sure your code lints.
6. Issue that pull request!

## Any contributions you make will be under the Apache 2.0 software licenses

In short, when you submit code changes, your submissions are understood to be under the
same [Apache 2.0 License](./LICENSE.txt) that covers the project. Feel free to contact the maintainers if that's a concern.

## Write bug reports with detail, background, and sample code

[This is an example](http://stackoverflow.com/q/12488905/180626).
Here's [another example from Craig Hockenberry](http://www.openradar.me/11905408).

**Great Bug Reports** tend to have:

-   A quick summary and/or background
-   Steps to reproduce
    -   Be specific!
    -   Give sample code if you can.
-   What you expected would happen
-   What actually happens
-   Notes (possibly including why you think this might be happening, or stuff you tried that didn't work)

People _love_ thorough bug reports. I'm not even kidding.

## License

By contributing, you agree that your contributions will be licensed under its Apache 2.0 License.

## Contributor Licensing Agreement

Before you can submit any code, all contributors must sign a
contributor license agreement (CLA). By signing a CLA, you're attesting
that you are the author of the contribution, and that you're freely
contributing it under the terms of the Apache-2.0 license.

The [individual CLA](https://tergite.github.io/contributing/icla.pdf) document is available for review as a PDF.

Please note that if your contribution is part of your employment or 
your contribution is the property of your employer, 
you will also most likely need to sign a [corporate CLA](https://tergite.github.io/contributing/ccla.pdf).

All signed CLAs are emails to us at <quantum-nextlabs@chalmers.se>.

## How to Test

- Ensure you have a [redis server](https://redis.io/docs/install/install-redis/) installed on your local machine,
  which the `quantify` extra's tests need.
- Ensure you have [uv](https://docs.astral.sh/uv/getting-started/installation/) installed. 
 (_You could simply have python +3.13 installed instead._)

```shell
wget -qO- https://astral.sh/uv/install.sh | sh
# or use curl
# curl -LsSf https://astral.sh/uv/install.sh | sh
```

- Clone the repo

```shell
git clone git@github.com:tergite/tergite-backend.git
cd tergite-backend
```

- Install dependencies. Note that we can either use the simulators (`qiskit`) or connect to Qblox clusters (`quantify`).
  To install for simulators, use `--extra qiskit` and to install for Qblox, use `--extra quantify`.  
  **Take note that you cannot use both at the same time because they conflict with each other**.

```shell
uv sync --dev --extra quantify
# or for simulators only
# uv sync --dev --extra qiskit
```

- Activate your environment

```shell
source .venv/bin/activate
```

- For the `quantify` extra, start the redis server in another terminal on port 6378

```shell
redis-server --port 6378
```

- Lint with black, isort and pyflakes, as CI does

```shell
black --check api libs services utils tests dev migrations main.py settings.py
isort --check-only api libs services utils tests dev migrations main.py settings.py
pyflakes api libs services utils tests dev migrations main.py settings.py
```

- Optionally enable the pre-commit hook to run black and isort

```shell
pre-commit install
```

- Run the tests by running the command below at the root of the project.
  `-n auto --dist=loadscope` runs them in parallel, but the booking tests count real
  seconds, so they can fail on a busy machine.

```shell
pytest tests
```

## How to Install as a Service

- Download the package for your machine from the release, a deb for amd64 or a pkg for macOS
  arm64: `tergite-backend` runs real devices through quantify, `tergite-backend-simulator` runs
  the qiskit simulators. They install side by side.

- Install it. A deb asks for a first backend instance: its name, which is its `DEFAULT_PREFIX`,
  executor, port, MSS URL and, for quantify, Redis.

```shell
sudo apt install ./tergite-backend_<version>_amd64.deb
```

- Create any other instance, or the first on macOS, which asks nothing.
  `--executor`, `--port`, `--mss-url` and `--redis-url` override the defaults, and a Redis
  password goes in `REDIS_PASSWORD`.

```shell
sudo bcc-provision lab1
# or for a simulator
# sudo bcc.simulator-provision sim1 --executor qiskit_pulse_2q --port 8002
```

- Edit the instance's configs in `/etc/bcc/<name>/` for the device, copy MSS's
  `public-mss-key.pem` there unless MSS runs on the same machine, and give MSS the
  `[[backends]]` entry and `public-bcc-key.pem` that provisioning printed.

- Start the instance, and on every boot. Its data is in `/var/lib/bcc/<name>/`.

```shell
sudo systemctl enable --now bcc@lab1
# or for a simulator: bcc.simulator@sim1
journalctl -u bcc@lab1 -f
```

## Build Executable with [Nuitka](https://nuitka.net/)

- Ensure you have [uv](https://docs.astral.sh/uv/getting-started/installation/) installed. 
 (_You could simply have python +3.13 installed instead._)

```shell
wget -qO- https://astral.sh/uv/install.sh | sh
# or use curl
# curl -LsSf https://astral.sh/uv/install.sh | sh
```

- Clone the repo

```shell
git clone git@github.com:tergite/tergite-backend.git
cd tergite-backend
```

- Build the main.py file, which installs the dependencies of the executor first

```shell
pkg/build.sh
# or if you want the simulators, executor is qiskit
# EXECUTOR=qiskit pkg/build.sh
```

- After the build, the app will be at `main.dist/bcc` (or `main.dist/bcc.simulator` if EXECUTOR=qiskit), in
  the folder it needs beside it. You can run it.

```shell
# serve the API, and run one heavy worker for preprocessing, executions and recalibrations
# and `--light-workers` (default 1) for postprocessing, stopping them all if
# any stops; it reads the .env file named by BCC_ENV_FILE, and takes `--port`, `--host`,
# `--forwarded-allow-ips` and `--workers`, the number of API processes ($WEB_CONCURRENCY or 1)
BCC_ENV_FILE="$PWD/.env" main.dist/bcc start  # or just main.dist/bcc, as start is the default
# or one at a time: `main.dist/bcc serve`, `main.dist/bcc worker --kind heavy`, `main.dist/bcc worker --kind light`

# serve several devices from one process: each device's env file once; its API is then at
# /<its DEFAULT_PREFIX>/..., say http://localhost:8000/loke/bookings, a path naming no device
# reaching the first; each device gets a heavy worker, and the light workers postprocess every
# device's jobs; in the shell, BCC_<NAME>, e.g.
# BCC_MAX_IDLE_TIME, sets every device's <NAME> over its env file, and BCC_<PREFIX>_<NAME>,
# e.g. BCC_LOKE_MAX_IDLE_TIME, one device's over that; see .env.example
main.dist/bcc start --env-file loke.env --env-file thor.env
# or, as BCC_ENV_FILE may name several separated as in PATH
BCC_ENV_FILE="$PWD/loke.env:$PWD/thor.env" main.dist/bcc start
```

## References

This document was adapted from [a gist by Brian A. Danielak](https://gist.github.com/briandk/3d2e8b3ec8daf5a27a62) which
was originally adapted from the open-source contribution guidelines
for [Facebook's Draft](https://github.com/facebook/draft-js/blob/a9316a723f9e918afde44dea68b5f9f39b7d9b00/CONTRIBUTING.md)
