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

A `[simulator]` section in it makes the device a simulated one, a chip behind dummy clusters; see
[Simulated Devices](#simulated-devices).

`max_clbits` in `[general_config]` is the most classical bits a circuit may have, what the
device's readout instruments can acquire per shot; it is 1024 if left out. A circuit that
declares more is refused at upload with 422.

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

The device publishes the limits to MSS as its `pulse_limits`, so that users can read them
before they write a job.

The commented section in [`backend_config.example.toml`](../backend_config.example.toml)
shows the shape; its values are placeholders, to be replaced with limits chosen for the chip,
its fridge and its amplifiers. `uv run python -m dev.check_gate_level <device>.env` warns of a
device that sets none.

#### Simulated Devices

A `[simulator.chip]` section in its `backend_config.toml` puts a simulated chip behind the
device's dummy clusters: its jobs compile for the clusters, as on hardware, and each schedule
plays on the chip's true parameters, which its calibration does not see, so the calibration
may be off as a real chip's is. Its quantify metadata lists dummy instruments only, and the
qubits' frequencies must lie within reach of their LOs in the quantify hardware config. The
`readout_iq` and `readout_noise` of the QuTiP simulators it replaced are refused.

- Each pulse plays at its clock's frequency, detuned from the chip's true frequency, on a
  transmon of three levels with the chip's anharmonicity, Rabi frequency, T1 and T2; a reset
  takes it to |0>.
- A measurement integrates its hanger resonator's response to the readout pulse: a dip at
  `frequency` with the transmon in |0>, moved by `dispersive_shift` in |1> and twice that in
  |2>, and towards `bare_frequency` as the readout amplitude nears `critical_amplitude`. An
  averaged acquisition reads the populations' mean response, a single-shot one a level drawn
  for each shot; each with `readout_noise` about it, for an integration of a microsecond.
- A coupler is a flux-tunable transmon of `max_frequency` and `anharmonicity`, coupled to
  each of its qubits, sitting at `flux_offset` flux quanta. Its flux pulse, on its `:fl`
  port, reaches it modulated at 4.4 GHz less its clock's frequency, `flux_per_amplitude`
  flux quanta at amplitude 1, which drives the parametric CZ: a modulation at the difference
  of |11> and |02> (or |20>) swaps them, and a full swap adds a phase of pi to |11>. Park it
  off its sweet spot, where the flux moves its frequency at the modulation's own frequency.
- Two coupled transmons pick up their parked coupler's static ZZ while idle, unless
  `should_simulate_static_zz = false`.
- If the quantify metadata lists an SPI rack, a dummy, the DC currents tergite-tuner sets on
  it move each coupler `flux_per_current` flux quanta an ampere, and its transmons'
  frequencies with it; without one, each coupler sits at its `flux_offset`, as at a current
  set and watched by hand.
- A measurement does not collapse the state, so a transmon is read once between its resets,
  after anything else plays on it.
- It recalibrates with tergite-tuner as a chip on clusters does, from its tuner configs:
  the device config's VNA frequencies are the rough guesses the tuner sweeps around, so the
  chip's true frequencies must lie within its sweeps of them.

```toml
[simulator.chip]
# the standard deviation of a shot's I, and of its Q, integrated over a microsecond
readout_noise = 0.002
# whether coupled transmons pick up their static ZZ while idle
should_simulate_static_zz = true

[simulator.chip.qubits.q12]
frequency = 4.5116e9        # Hz, of |0> to |1>
anharmonicity = -0.21e9     # Hz, of |1> to |2> less that of |0> to |1>
rabi_frequency = 1.62e8     # Hz, of a resonant drive of amplitude 1
t1 = 6e-5                   # s
t2 = 5e-5                   # s, at most twice t1

[simulator.chip.resonators.q12]
frequency = 7.2601e9        # Hz, its dip with the transmon in |0>, at a weak readout
dispersive_shift = -0.5e6   # Hz, of its dip with the transmon in |1>
bare_frequency = 7.2581e9   # Hz, its dip at a readout too strong for the transmon to move it
critical_amplitude = 0.4    # the readout amplitude that moves the dip half way there
internal_quality = 60000
coupling_quality = 20000
amplitude = 0.3             # its transmission off resonance, at a readout of amplitude 1
phase = 0.4                 # rad

[simulator.chip.couplers.q12_q13]
max_frequency = 7.5e9       # Hz, at zero flux
anharmonicity = -0.15e9     # Hz
flux_offset = 0.25          # flux quanta it sits at, without a DC current
flux_per_current = 100.0    # flux quanta of an ampere of DC current
flux_per_amplitude = 0.1    # flux quanta of a flux pulse of amplitude 1

[simulator.chip.couplers.q12_q13.couplings]
q12 = 0.1e9                 # Hz
q13 = 0.1e9
```

The test fixtures hold two simulated devices, `sim_1q`, a transmon, and
`sim_2q`, two and their coupler, with the dummy cluster they play behind and
tergite-tuner's configs to recalibrate them. Their seeds are what the tuner found from the
rough guesses of `chip_calib_device_config.toml`; each recalibrates, in a minute or three, as a
device on clusters does: an interval after it starts, or as it starts where
`BCC_SHOULD_RECALIBRATE_AT_START=true` (`tests/services/test_simulated_chip_calibration.py`
calibrates the two-qubit one from `sim_2q.rough.seed.toml`):

```shell
# on the root of the project
cp tests/fixtures/backend_config.simq2.toml backend_config.toml
cp tests/fixtures/sim_2q.seed.toml calibration.seed.toml
cp tests/fixtures/dummy-quantify-config.json quantify-config.json
cp tests/fixtures/dummy-quantify-metadata.yml quantify-metadata.yml
cp tests/fixtures/chip_calib_device_config.toml device_config.calib.toml
cp tests/fixtures/chip_node_config.toml node_config.calib.toml
cp tests/fixtures/dummy_spi_config.calib.toml spi_config.calib.toml
./dev/start_bcc.sh
```
