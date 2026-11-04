# Tergite Backend (formerly Tergite BCC)

![CI](https://github.com/tergite/tergite-backend/actions/workflows/ci.yml/badge.svg)

The Backend in the [Tergite software stack](https://tergite.github.io/) of the WACQT quantum computer.

**This project is developed by a core group of collaborators.**    
**Chalmers Next Labs AB (CNL) takes on the role of managing and maintaining this project.**

## Version Control

The tergite stack is developed on a separate version control system and mirrored on Github.
If you are reading this on GitHub, then you are looking at a mirror. 

## Dependencies

- [Python 3.13](https://www.python.org/)

## Quick Start

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

- Install dependencies, the same for a device on Qblox clusters and for a simulated one

```shell
uv sync --dev
```

- Activate your environment

```shell
source .venv/bin/activate
```

- If you don't have a key certificate pair for MSS, generate them on the MSS machine 
  and copy the public key certificate to the backend machine in this root folder.

```shell
## use param -aes-256-cbc for password-protection
openssl genpkey -algorithm RSA -out private-mss-key.pem -pkeyopt rsa_keygen_bits:4096
openssl rsa -pubout -in private-mss-key.pem -out public-mss-key.pem
# scp public-mss-key.pem backend-host:~/tergite-backend/
```

- If you don't have a key certificate pair for BCC, generate them her 
  and copy the public key certificate to the MSS machine in this `bcc_certs` folder and update its `mss-config.toml`

```shell
## use param -aes-256-cbc for password-protection
openssl genpkey -algorithm RSA -out private-bcc-key.pem -pkeyopt rsa_keygen_bits:4096
openssl rsa -pubout -in private-bcc-key.pem -out public-bcc-key.pem
# scp public-bcc-key.pem backend-host:~/tergite-frontend/bcc_certs
# then update the public_key_path for the backend in mss-config.toml of MSS
```

- Copy the `.env.example` file to `.env` and update the environment variables there appropriately.

```shell
cp .env.example .env
```

- **For a device on Qblox clusters**, copy the quantify example config file `quantify-config.example.json` and `quantify-metadata.example.yml` into 
 the `quantify-config.json` and `quantify-metadata.yml` file and update the variables there in.    
 **Ignore this for a simulated device**, which needs neither

```shell
cp quantify-config.example.json quantify-config.json
cp quantify-metadata.example.yml quantify-metadata.yml 
```

_Note: If you want to just run a dummy cluster, you can copy the one in the test fixtures_

```shell
cp tests/fixtures/generic-quantify-config.json quantify-config.json
cp tests/fixtures/generic-quantify-config.yml quantify-metadata.yml
```

- Copy the backend example config file `backend_config.example.toml` into the `backend_config.toml` file and update the variables there in.

```shell
cp backend_config.example.toml backend_config.toml
```

_Note: For a simulated device, a chip behind dummy clusters, you can copy one of the test fixtures, with its
calibration seed, dummy cluster and tergite-tuner configs; its `[simulator]` section is what makes it simulated
(see [docs/configuration.md](docs/configuration.md#simulated-devices))_

```shell
cp tests/fixtures/backend_config.simq1.toml backend_config.toml
cp tests/fixtures/qiskit_pulse_1q.seed.toml calibration.seed.toml
cp tests/fixtures/dummy-quantify-config.json quantify-config.json
cp tests/fixtures/dummy-quantify-metadata.yml quantify-metadata.yml
cp tests/fixtures/chip_calib_device_config.toml device_config.calib.toml
cp tests/fixtures/chip_node_config.toml node_config.calib.toml
cp tests/fixtures/dummy_spi_config.calib.toml spi_config.calib.toml
```

- Copy the example file for initial device calibrations `calibration.seed.example.toml` into the `calibration.seed.toml` file and update the variables accordingly. 

```shell
cp calibration.seed.example.toml calibration.seed.toml
```

- Run start script

```shell
./dev/start_bcc.sh
```

- Open your browser at [http://localhost:8000/docs](http://localhost:8000/docs) to see the interactive API docs

## Documentation

Find how the backend is built, and the conventions to follow when changing it, in [DESIGN.md](./DESIGN.md),
and more documentation in the [docs folder](./docs)

## Contribution Guidelines

If you would like to contribute, please have a look at our
[contribution guidelines](./CONTRIBUTING.md)

## Authors

This project is a work of
[many contributors](https://github.com/tergite/tergite-backend/graphs/contributors).

Special credit goes to the authors of this project as seen in the [CREDITS](./CREDITS.md) file.

## ChangeLog

To view the changelog for each version, have a look at
the [CHANGELOG.md](./CHANGELOG.md) file.

## License

[Apache 2.0 License](./LICENSE.txt)

## Acknowledgements

This project was sponsored by:

-   [Knut and Alice Wallenberg Foundation](https://kaw.wallenberg.org/en) under the [Wallenberg Center for Quantum Technology (WACQT)](https://www.chalmers.se/en/centres/wacqt/) project at [Chalmers University of Technology](https://www.chalmers.se)
-   [Nordic e-Infrastructure Collaboration (NeIC)](https://neic.no) and [NordForsk](https://www.nordforsk.org/sv) under the [NordIQuEst](https://neic.no/nordiquest/) project
-   [European Union's Horizon Europe](https://research-and-innovation.ec.europa.eu/funding/funding-opportunities/funding-programmes-and-open-calls/horizon-europe_en) under the [OpenSuperQ](https://cordis.europa.eu/project/id/820363) project
-   [European Union's Horizon Europe](https://research-and-innovation.ec.europa.eu/funding/funding-opportunities/funding-programmes-and-open-calls/horizon-europe_en) under the [OpenSuperQPlus](https://opensuperqplus.eu/) project
