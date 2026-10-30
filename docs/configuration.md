# Configuration

Documentation about configuring BCC

## General Configuration

To configure the entire application, we use `.env` files.   

Just copy the [`.env.example`](../.env.example) to `.env` and update the variables there in.

```shell
cp .env.example .env
```

## QBLOX Instruments Configuration

We use two templates for how to configure this application to control the
[QBLOX instruments](https://qblox-qblox-instruments.readthedocs-hosted.com/en/main/index.html) that control the quantum computer:
[`quantify-config.example.json`](../quantify-config.example.json), quantify-scheduler's hardware compilation config,
and [`quantify-metadata.example.yml`](../quantify-metadata.example.yml), the clusters' addresses and modules.

Just copy them to `quantify-config.json` and `quantify-metadata.yml` and update their variables and you are good to go.

```shell
# on the root of the project
cp quantify-config.example.json quantify-config.json
cp quantify-metadata.example.yml quantify-metadata.yml
```

### Dummy QBLOX Instrumments

You may wish to run some dummy QBLOX instruments if you don't have access to the physical QBLOX instruments

We already have a preconfigured [`dummy-quantify-config.json`](../tests/fixtures/dummy-quantify-config.json) and
[`dummy-quantify-metadata.yml`](../tests/fixtures/dummy-quantify-metadata.yml) for this in the `tests/fixtures` folder.

Copy them to your root folder.

```shell
# on the root of the project
cp tests/fixtures/dummy-quantify-config.json quantify-config.json
cp tests/fixtures/dummy-quantify-metadata.yml quantify-metadata.yml
```

_NOTE: You can find out more about the configuration properties in the quantify-config file by 
visiting the [quantify_scheduler docs](https://quantify-os.org/docs/quantify-scheduler/dev/reference/qblox/Cluster.html)
and the [QCoDeS drivers docs](https://microsoft.github.io/Qcodes/)._  

_NOTE: You could choose to use different names for these files e.g. `foobar.json`.
You however need to explicitly set these names in the `.env` file, e.g. `BCC_QUANTIFY_CONFIG_FILE=foobar.json`
and `BCC_QUANTIFY_METADATA_FILE=foobar.yml`_  

### General Backend Configuration

We configure all backends using the `backend_config.toml`.   
We use the [`backend_config.example.toml`](../backend_config.example.toml) as a template.  

This configuration file can contain the calibration values in case we are running it as a simulator.
The calibration values are under the key `simulator_config`. 

**Note: the `simulator_config` is ignored if the `general_config.simulator` variable is not set to `true`.**    

_NOTE: You don't need to pass the `.env` file, the `backend_config.toml` file or the quantify files to the 
start script as these are automatically loaded for you._  

#### Single-qubit-gate Qiskit Pulse Simulator

You may wish to run a single-qubit simulator.  

First update the `.env` file to contain `BCC_EXECUTOR_TYPE=qiskit_pulse_1q`.  

We already have a preconfigured [`backend_config.simq1.toml`](../tests/fixtures/backend_config.simq1.toml) for this in the 
`tests/fixtures` folder.   

Copy it to your root folder.

```shell
# on the root of the project
cp tests/fixtures/backend_config.simq1.toml backend_config.toml
```

And run the application.

```shell
./dev/start_bcc.sh
```

#### Two-qubit-gate Qiskit Pulse Simulator

You may wish to run a two-qubit-gate simulator.  

First update the `.env` file to contain `BCC_EXECUTOR_TYPE=qiskit_pulse_2q`.  

We already have a preconfigured [`backend_config.simq2.toml`](../tests/fixtures/backend_config.simq2.toml) for this in the 
`tests/fixtures` folder.   

Copy it to your root folder.

```shell
# on the root of the project
cp tests/fixtures/backend_config.simq2.toml backend_config.toml
```

And run the application.

```shell
./dev/start_bcc.sh
```