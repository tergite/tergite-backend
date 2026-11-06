# Executors

The executor compiles a job's circuits into the device's native programs and runs them. This
page is for contributors who change how circuits compile or run. What the backend does to a
circuit that can change a job's *results* is in [how_circuits_run.md](how_circuits_run.md).

`QuantifyExecutor` (`libs/quantum_executor/quantify/`) is the only executor, and
`BCC_EXECUTOR_TYPE=quantify` the only type. It schedules with quantify-scheduler on Qblox
clusters, real or dummy. A simulated device is a simulated chip behind dummy clusters (see
[below](#the-simulated-chip)).

## What an executor does

A `QuantumExecutor` (`libs/quantum_executor/base/executor.py`) holds the state a device's jobs
run with: the instruments' coordinator, or the simulated chip. It implements:

| Method | Does |
| --- | --- |
| `to_circuit_experiments` | compiles a job's circuits into experiments |
| `run_native` | runs the experiments and returns the IQ points |
| `recalibrate` | recalibrates the device, or returns `None` |
| `close` | releases the instruments |

The module functions `preprocess_circuits` and `run_job` drive it, in the heavy worker:

1. **Preprocessing** compiles the job and estimates its duration, which the booking rules need.
2. **Execution** runs it and writes the IQ points.
3. **Postprocessing**, in a light worker, discriminates the IQ points into each shot's
   classical register.

The executor is imported only when a heavy worker builds one, so the API never pays the
seconds quantify takes to import.

## Gate-level jobs

A job file's `params` (`CircuitJobParams`, in `services/scheduler/dtos.py`) hold OpenQASM 3
circuits, with `shots`, `meas_level` (1 or 2) and `meas_return`. Each circuit has a `name`, its
`qasm` and, optionally, `num_clbits`: if given, it must match the classical bits the OpenQASM
declares; if not, the circuit has those.

1. **Parse.** `base/qasm.py` parses each circuit with `openpulse`.
2. **Translate to native gates** (`base/gates.py`): `Rxy`, `Rz`, `CZ`, `Measure`, `Reset`,
   `Delay`, `Barrier`. Other gates are decomposed by a built-in table, by the program's own
   gate definitions, or by OpenQASM's `stdgates.inc`. Control flow and gate modifiers are
   refused.
3. **Map qubits.** Circuit qubit `i` is the device's `i`-th qubit, by the number in its id.
4. **Compile** with the device's calibration, against a quantify `QuantumDevice` with a
   transmon per qubit and a `CouplerEdge` per coupler (`quantify/device.py`).
   `quantify/circuits.py` adds the gates' operations in order. A step of quantify's device
   compilation (`quantify/scheduling.py`) then times them, as-late-as-possible. See
   [how_circuits_run.md](how_circuits_run.md) for the rules.

A circuit that measures no qubit is refused, since it has no results.

**Refused at upload.** The upload translates each circuit into native gates (steps 1 and 2),
so a circuit that does not translate is refused with 422 before the job is created: one that
is not valid OpenQASM 3, a gate given the wrong parameters or qubits, a statement or gate that
is not supported, or a `num_clbits` other than the OpenQASM's. The `detail` gives the reason,
the circuit's index in the job file and, for a syntax error outside a `cal` block, its line
(from 1) and column (from 0); they are `null` for any other error:

```json
{
  "detail": {
    "message": "circuit bell is not valid OpenQASM 3: line 4, column 0: missing ';' at 'cx'",
    "circuit_index": 0,
    "line": 4,
    "column": 0
  }
}
```

What depends on the device and its calibration, such as a CZ on qubits without a coupler, an
uncalibrated qubit or more qubits than the device has, fails the job in preprocessing, with
the reason as its `failure_reason`.

**Readout.** Each measurement is acquired on its own channel, numbered in the order the
circuit measures. The `CircuitReadout` saved with the results maps each channel to its qubit
and classical bit. Postprocessing puts each measurement in its bit (bit 0 least
significant); a later measurement of a bit replaces an earlier one.

**Old SDKs.** A job file holding a `qobj` (the Pulse Qobj that SDKs before gate-level jobs
sent) is refused at upload with 400, asking the user to upgrade the SDK.

## Pulse-level jobs

A circuit may hold OpenPulse (`base/pulses.py`):

- a `cal` block, or
- a `defcal` that replaces the gate it matches by name, physical qubits and parameters.

These play on the frames the device publishes, named after its ports (`q12_drive`,
`q12_readout`, `q12_q13_coupler`). They support waveform generators or sample arrays, phase
and frequency shifts and sets, delays, barriers, and `capture_v1` into a classical bit (read
out as a measurement of the frame's qubit).

**Who may send one.** Pulse-level jobs are allowed only when both hold:

- MSS says the user's project may send them. It says so when it asks for the job token,
  which carries a `pulse` claim (`MSSTokenClaims.is_pulse_allowed`).
- The device's backend config sets `pulse_limits` (`base/pulse_limits.py`): amplitude and
  energy per kind of frame, how far a frame's frequency may move, samples per waveform, and
  the duration of a capture and of a shot's pulse-level blocks. A device without them takes
  no pulse-level jobs. See [configuration.md](configuration.md#pulse-level-limits).

Both are checked at upload. The limits are checked again before the job runs.

**Published `defcal`s.** A device that takes pulse-level jobs publishes its frames, and its
`x`, `sx`, `rz` and `cz` as `defcal`s, with its calibration (`quantify/defcals.py`). Each is
the gate compiled on the device and written out as samples, delays and phase shifts, so a job
that copies one plays exactly what the gate plays. `measure` is not published, as jobs may
not define it.

**Drives whose LO sits below the qubit.** The clusters turn phases the other way on such a
drive. The executor negates `Rz` angles, `Rxy` axes and a `cal` block's phase shifts on that
drive (`get_mirrored_elements`), and the published `defcal`s carry the negated phase shifts.

## Checking a device's configs

`dev/check_gate_level.py` checks a device's configs for gate-level jobs without touching its
instruments. It reports missing calibration and LDA values, couplers whose CZ plays no pulse,
and hardware config that the compiler refuses or that would put an NCO beyond ±500 MHz.

## The simulated chip

A backend config's `[simulator.chip]` section puts a simulated chip behind the device's dummy
clusters (`quantify/simulated_chip/`). The chip has true parameters that the device's
calibration does not see, so the calibration may be off, as on a real chip. How to configure
one is in [configuration.md](configuration.md#simulated-devices).

**How it plugs in.** The executor gives each dummy cluster a `SimulatingClusterComponent` in
place of quantify's `ClusterComponent`. When the coordinator prepares a schedule, the
component plays it on the chip (`engine.py`) and loads the cluster's acquisition bins with
the result. The coordinator's `start` and `retrieve_acquisition` then return it through
quantify's own code path.

**What it models.**

| Part | Model | Code |
| --- | --- | --- |
| Transmon | three levels, with T1 and T2; each pulse plays at its clock's frequency, detuned from the true one | `transmon.py` |
| Readout | a hanger resonator's response to the readout pulse, per level; averaged or drawn shot by shot, with noise | `readout.py` |
| Coupler | a flux-tunable transmon between two qubits; a flux pulse at the right frequency swaps \|11> and \|02>, giving the CZ's phase | `coupler.py` |
| Coupled qubits | evolve as one register, with static ZZ while idle (switchable) | `register.py` |
| SPI rack | if listed in the quantify metadata, its DC currents park each coupler and shift its transmons | |

A reset takes a transmon to |0>. A loop's body plays once, with each iteration read out from
it. A measurement does not collapse the state, so a gate on a qubit after its measurement,
before a reset, is refused.

**Recalibration.** A simulated device recalibrates with tergite-tuner like a device on real
clusters, using the tuner's real-cluster mode (its dummy mode would fake each node's
acquisitions). While the tuner runs, `simulated_chip/tuner.py` swaps the `Cluster` and
`ClusterComponent` that the tuner imports for the dummy cluster and the simulating component.
A test fails if the tuner stops connecting through them.

## Adding an executor

1. **Create the class.** Add a package beside `quantify` in `libs/quantum_executor/`, with a
   subclass of `QuantumExecutor` that implements the four methods above. If the backend needs
   a tune-up, such as training a discriminator, do it in the constructor and put the result
   in its `backend_config`; the heavy worker publishes that config to MSS once the executor
   is loaded.
2. **Compile circuits.** `to_native_circuit` (`base/qasm.py`) gives you native gates. Compile
   them, with the device's calibration, into what your backend runs, acquiring each `Measure`
   on its `channel`. Return them as `CircuitExperiment`s whose readout
   `NativeCircuit.to_readout` gives. Refuse anything you cannot play, such as a `PulseBlock`,
   with an `UnsupportedCircuitError`.
3. **Load it** in `_init_executor` in `services/scheduler/utils.py`, by a new
   `BCC_EXECUTOR_TYPE` (`executor_type` in `utils/config.py`) or by a section of the backend
   config.
4. **Wire it up.** Add its dependencies to `pyproject.toml`. Add its settings to
   `.env.example` and example configs beside `quantify-config.example.json`, and have
   `pkg/provision.sh` write them for a new instance. Document it here.
5. **Test it.** Run the whole suite.
