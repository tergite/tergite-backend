## Adding a QuantumExecutor

The executor is the core component to run the quantum circuit on a backend.
It manages the access to the instruments or sets up the simulator in case the backend is a simulator.
Right now, `BCC_EXECUTOR_TYPE=quantify` is the only executor type, and `QuantifyExecutor`, in
`libs/quantum_executor/quantify/executor.py`, the only executor: quantify-scheduler schedules on Qblox clusters, real or
dummy; check out the [configuration manual](./configuration.md) on how to run dummy ones. A `[simulator.chip]` section in
a backend config puts a simulated chip behind its dummy clusters; see [Simulated Devices](./configuration.md#simulated-devices).

If you want to implement a new executor, follow these steps below.

### 1. Create a class for your executor
- The base class for any executor is `QuantumExecutor`, in `libs/quantum_executor/base/executor.py`.
- Add a new package for your executor in `libs/quantum_executor/`, beside `quantify`.
- Inherit from that class to create your own executor, implementing `to_circuit_experiments`, `run_native`,
  `recalibrate` (returning `None` if your backend does not recalibrate) and `close`.
- If your backend needs some basic tune-up or training, such as a discriminator, do it when the executor is
  constructed and put the result in its `backend_config`; the heavy worker publishes
  that config to MSS once the executor is loaded.

### 2. Implement a transpiler
- Incoming jobs are OpenQASM 3 circuits, which may hold OpenPulse `cal` blocks and `defcal`s.
- `to_native_circuit`, in `libs/quantum_executor/base/qasm.py`, translates a circuit into native gates
  (`libs/quantum_executor/base/gates.py`); compile those, with the device's calibration, into the schedule your
  backend runs, acquiring each `Measure` on its `channel`, and return them as `CircuitExperiment`s whose readout
  `NativeCircuit.to_readout` gives. `run_native` runs each of them.
- A backend that cannot play pulse-level blocks (`PulseBlock`, in `libs/quantum_executor/base/gates.py`), or another
  part of a circuit, refuses it with an `UnsupportedCircuitError`, as the simulated chip does a gate after a measurement in
  `libs/quantum_executor/quantify/simulated_chip/engine.py`.
- Other modules in `libs/quantum_executor` might have to be adapted or added.

### 3. Next steps
- Load your executor in `_init_executor` in `services/scheduler/utils.py`, by a new `BCC_EXECUTOR_TYPE` (the
  `executor_type` of `DeviceSettings`, in `utils/config.py`) or by a section of the backend config.
- Add its dependencies to `pyproject.toml`, and to the Nuitka options at the top of `main.py`.
- Add documentation for your executor.
  - You probably have to update the `.env.example`, and add example configs beside `quantify-config.example.json`.
  - Provisioning, in `pkg/provision.sh`, writes an instance's example configs for its device.
- Check whether all tests are running.

For more intuition on how the executors are integrated into the stack, check out the existing ones in the source code.
