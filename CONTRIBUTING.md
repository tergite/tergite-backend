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

- Ensure you have a [redis server](https://redis.io/docs/install/install-redis/) installed on your local machine.
- Ensure you have [uv](https://docs.astral.sh/uv/getting-started/installation/) installed. 
 (_You could simply have python +3.12 installed instead._)

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
  **Take note that you can use both at the same time because they conflict with each other**.

```shell
uv sync --dev --extra quantify
# or for simulators only
# uv sync --dev --extra qiskit
```

- Activate your environment

```shell
source .venv/bin/activate
```

- Start the redis server in another terminal on port 6378

```shell
redis-server --port 6378
```

- Lint with black

```shell
black --check app
```

- Optionally enable pre-commit hook to run black 

```shell
pre-commit install
```

- Run the tests by running the command below at the root of the project. 

```shell
pytest -n auto --dist=loadscope app
```

## How to Run With Systemd

- Clone the repo

```shell
git clone git@github.com:tergite/tergite-backend.git
```

- Copy the `dot-env-template.txt` into the `.env` file and update the variables there in. Contact your teammates for
 the variables you are not sure of.

```shell
cd tergite-backend
cp dot-env-template.txt .env
```

- Copy the `quantify-config.example.json` into `quantify-config.json` and update the variables there in.

```shell
cp quantify-config.example.json quantify-config.json
```

- Copy the `quantify-metadata.example.yml` into `quantify-metadata.yml` and update the variables there in.

```shell
cp quantify-metadata.example.yml quantify-metadata.yml 
```

- If you need some seed data (mandatory if you are to use a simulator), copy the `calibration.seed.example.toml` into `calibration.seed.toml` and update the variables there in.

```shell
cp calibration.seed.example.toml calibration.seed.toml
```

- Copy `bcc.service` to the systemd services folder

```shell
sudo cp bcc.service /etc/systemd/system/bcc.service
```

- Get the path to your conda bin:

```shell
YOUR_CONDA_BIN_PATH="$(conda info --base)/bin"
```


- Extract also the path to this folder where `tergite-backend` is.

```shell
YOUR_PATH_TO_BCC=$(pwd)
```

- Get also the current user

```shell
YOUR_USER=$(whoami)
```

- Replace the variables `YOUR_CONDA_BIN_PATH` and `YOUR_PATH_TO_BCC` with the right values in `/etc/systemd/system/bcc.service`

```shell
sudo sed -i.bak "s:YOUR_USER:${YOUR_USER}:" /etc/systemd/system/bcc.service
sudo sed -i.bak "s:YOUR_CONDA_BIN_PATH:${YOUR_CONDA_BIN_PATH}:" /etc/systemd/system/bcc.service
sudo sed -i.bak "s:YOUR_PATH_TO_BCC:${YOUR_PATH_TO_BCC}:" /etc/systemd/system/bcc.service
sudo rm /etc/systemd/system/bcc.service.bak
```

- Start BCC service

```shell
sudo systemctl start bcc.service
```

- Check the BCC service status

```shell
sudo systemctl status bcc.service
```

- Enable BCC to start on startup incase the server is ever restarted.


```shell
sudo systemctl enable bcc.service
```

## How to Run with Docker

- Ensure you have [Docker](https://docs.docker.com/engine/install/) installed.
- Create a `data` folder

```shell
mkdir data
```

- Create a proper `.env` file based on the `dot-env-template.txt` and put it in the `data` folder.

```shell
cp dot-env-template.txt data/.env
```

- Create a proper `quantify-config.yml` file if it is needed (i.e. if you don't want to run any of the simulators). 
  Put it in the `data` folder.

```shell
cp quantify-config.example.yml data/quantify-config.yml
```

- Create a proper `quantify-metadata.yml` if it is needed (i.e. if you don't want to run any of the simulators).
  Put it in the `data` folder.

```shell
cp quantify-metadata.example.yml quantify-metadata.yml 
```

- If you need some seed data (mandatory if you are to use a simulator), create the `calibration.seed.toml` file.
  Put it in the `data` folder.

```shell
cp calibration.seed.example.toml calibration.seed.toml
```

- Create a proper `backend_config.toml` file. Put it in the `data` folder.

```shell
cp backend_config.example.toml data/backend_config.toml
```

- Run the docker container with the `data` folder mounted as a volume.

```shell
docker run --name tg-backend -v ./data:/data -e ENV_FILE="/data/.env" -e BACKEND_SETTINGS="/data/backend_config.toml" tergite/tergite-backend:latest 
```

## References

This document was adapted from [a gist by Brian A. Danielak](https://gist.github.com/briandk/3d2e8b3ec8daf5a27a62) which
was originally adapted from the open-source contribution guidelines
for [Facebook's Draft](https://github.com/facebook/draft-js/blob/a9316a723f9e918afde44dea68b5f9f39b7d9b00/CONTRIBUTING.md)
