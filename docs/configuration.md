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

A `[simulator]` section in it makes the device a simulated one, run on QuTiP instead of clusters; see
[Simulated Devices](#simulated-devices).

_NOTE: You don't need to pass the `.env` file, the `backend_config.toml` file or the quantify files to the 
start script as these are automatically loaded for you._  

#### Pulse-level Limits

A device runs pulse-level jobs, OpenQASM 3 with OpenPulse `cal` blocks and `defcal`s, only
from the projects MSS lets send them, and only within the limits its operators set in the
`[pulse_limits]` section of its `backend_config.toml`. A device without the section refuses
every pulse-level job; gate-level jobs run either way.

Each job is checked at upload, refused with the reason if it is beyond a limit, and checked
again before it runs. The limits:

| Key | Unit | What it bounds |
| --- | --- | --- |
| `max_amplitude` | fraction of full scale, by kind | the largest \|sample\| of a waveform |
| `max_frequency_offset` | Hz | how far `set_frequency`/`shift_frequency` move a frame from its calibrated frequency |
| `max_energy_per_shot` | full scale² · s, by kind | the sum of \|sample\|² · dt on one frame in one shot |
| `max_energy_per_job` | full scale² · s, by kind | the same over the job: per-shot energy × shots, summed over circuits |
| `max_pulse_duration` | s | a circuit's pulse-level blocks in one shot |
| `max_samples_per_waveform` | samples | one waveform, as played |
| `max_capture_duration` | s | one capture |

The limits by kind take a kind of frame each: `drive`, `readout` or `coupler`. A kind left
out of any of them may not be played at all, so leaving out `coupler` keeps jobs off the
couplers, as on a chip whose couplers are parked by hand.

For a sense of the energies: a constant pulse of 0.3 for 100 ns is 0.3² × 100e-9 = 9e-9; at
1000 shots in each of two circuits, the job puts 1.8e-5 on that frame.

The commented section in [`backend_config.example.toml`](../backend_config.example.toml)
shows the shape; its values are placeholders, to be replaced with limits chosen for the chip,
its fridge and its amplifiers. `uv run python -m dev.check_gate_level <device>.env` warns of a
device that sets none.

#### Simulated Devices

A device whose `backend_config.toml` has a `[simulator]` section runs its jobs on QuTiP, not on
clusters: each circuit, gate-level or pulse-level, is compiled as on a chip, from the
calibration seed, and the pulses it plays drive a model of the chip.

- Each transmon the circuit touches has three levels, in the frame of its drive clock, and is
  driven at the Rabi rate that makes its calibrated pi pulse a pi rotation; DRAG, the
  anharmonicity (-200 MHz where the seed gives none) and leakage act as they would.
- A coupler's pulse switches on a ZZ interaction between its two transmons, scaled so that
  the calibrated CZ adds a phase of pi to |11>; a CZ that plays no coupler pulse entangles
  nothing, and its phase corrections should be 0.
- T1 and T2 come from the seed.
- A measurement reads the state, without collapsing it, into IQ points about each qubit's
  points for states 0 and 1, from which the device's discriminators are drawn; so each
  qubit is measured once, after anything else plays on it, and a level above 0 reads as 1.

```toml
[simulator]
# the standard deviation of a shot's I, and of its Q, about its state's point
readout_noise = 0.1

[simulator.readout_iq]
# each qubit's IQ point of state 0 and of state 1, by its id
q0 = [[1.0, 0.0], [-1.0, 0.0]]
q1 = [[0.0, 1.0], [0.0, -1.0]]
```

The test fixtures hold a simulated one-qubit device and a two-qubit one with a coupler, which
run with the default `BCC_EXECUTOR_TYPE=quantify`; a simulated device needs no quantify hardware
config or metadata and none of tergite-tuner's configs:

```shell
# on the root of the project
cp tests/fixtures/backend_config.simq2.toml backend_config.toml
cp tests/fixtures/qiskit_pulse_2q.seed.toml calibration.seed.toml
./dev/start_bcc.sh
```
