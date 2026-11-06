# Tergite Backend (formerly Tergite BCC)

![CI](https://github.com/tergite/tergite-backend/actions/workflows/ci.yml/badge.svg)

The Backend in the [Tergite software stack](https://tergite.github.io/) of the WACQT quantum computer.
It runs quantum jobs on devices controlled by Qblox clusters, or on simulated devices.

**This project is developed by a core group of collaborators.**    
**Chalmers Next Labs AB (CNL) takes on the role of managing and maintaining this project.**

## Version Control

The tergite stack is developed on a separate version control system and mirrored on Github.
If you are reading this on GitHub, then you are looking at a mirror. 

## Dependencies

- [Python 3.13](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/)

## Quick Start

This starts the backend from a checkout with a simulated one-qubit device, so it needs no
hardware. To install it as a service instead, see [docs/deployment.md](./docs/deployment.md).

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/), if you don't have it.

```shell
curl -LsSf https://astral.sh/uv/install.sh | sh
```

- Clone the repo

```shell
git clone git@github.com:tergite/tergite-backend.git
cd tergite-backend
```

- Install the dependencies and activate the environment

```shell
uv sync --dev
source .venv/bin/activate
```

- Generate the backend's key pair. MSS checks the backend's events with its public key.

```shell
openssl genpkey -algorithm RSA -out private-bcc-key.pem -pkeyopt rsa_keygen_bits:4096
openssl rsa -pubout -in private-bcc-key.pem -out public-bcc-key.pem
```

- Put MSS's public key, `public-mss-key.pem`, in this folder: copy it from the MSS machine, or, if you
  are not running MSS, generate a throwaway pair here.

```shell
openssl genpkey -algorithm RSA -out private-mss-key.pem -pkeyopt rsa_keygen_bits:4096
openssl rsa -pubout -in private-mss-key.pem -out public-mss-key.pem
```

- Copy the example environment file. It expects MSS at `http://localhost:8002`; change
  `BCC_MSS_MACHINE_ROOT_URL` if yours is elsewhere.

```shell
cp .env.example .env
```

- Copy the configs of the simulated device

```shell
cp tests/fixtures/backend_config.simq1.toml backend_config.toml
cp tests/fixtures/sim_1q.seed.toml calibration.seed.toml
cp tests/fixtures/dummy-quantify-config.json quantify-config.json
cp tests/fixtures/dummy-quantify-metadata.yml quantify-metadata.yml
cp tests/fixtures/chip_calib_device_config.toml device_config.calib.toml
cp tests/fixtures/chip_node_config.toml node_config.calib.toml
cp tests/fixtures/dummy_spi_config.calib.toml spi_config.calib.toml
```

- Start the backend

```shell
./dev/start_bcc.sh
```

- Open [http://localhost:8000/docs](http://localhost:8000/docs) to see the interactive API docs.

Without MSS running, the backend still starts; it keeps its events and sends them once MSS is up.
To run a real device on Qblox clusters, see [docs/configuration.md](./docs/configuration.md).

## Documentation

| Document | Read it to |
| --- | --- |
| [DESIGN.md](./DESIGN.md) | understand how the backend is built, before changing it |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | run the tests and follow the code conventions |
| [docs/configuration.md](./docs/configuration.md) | configure a device, real or simulated |
| [docs/deployment.md](./docs/deployment.md) | install and run the backend as a service |
| [docs/how_circuits_run.md](./docs/how_circuits_run.md) | know what the backend does to a circuit that can change its results |
| [docs/booking_and_queuing.md](./docs/booking_and_queuing.md) | know how bookings decide which job runs |
| [docs/](./docs) | dig into the internals: the dispatcher, MSS events, auth, executors |

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
