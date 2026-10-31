## Adding a QuantumExecutor

The executor is the core component to run the quantum circuit on a backend.
It manages the access to the instruments or sets up the simulator in case the backend is a simulator.
Right now, we have implemented the following backends:
- "quantify": This is interfacing to qblox quantify via quantify and the recommended option, because the WACQT quantum processor is running on that platform. It can be run in the dummy mode, check out the [configuration manual](./configuration.md) on how to do it.
- "qiskit_pulse_1q": Single qubit simulator using qiskit-dynamics
- "qiskit_pulse_2q": Two-qubit gate simulator using qiskit-dynamics

If you want to implement a new executor, follow these steps below.

### 1. Create a class for your executor
- The base class for any executor is `QuantumExecutor`, in `libs/quantum_executor/base/executor.py`.
- Add a new package for your executor in `libs/quantum_executor/`, beside `quantify` and `qiskit`.
- Inherit from that class to create your own executor, implementing `to_native_experiments`, `run_native`,
  `recalibrate` (returning `None` if your backend does not recalibrate) and `close`.
- If your backend needs some basic tune-up or training, such as a discriminator, do it when the executor is
  constructed and put the result in its `backend_config`, as the qiskit simulators do; the heavy worker publishes
  that config to MSS once the executor is loaded.

### 2. Implement a transpiler
- Incoming jobs will be in the OpenPulse format.
- Implement a logic to translate the OpenPulse circuit to the format which your backend takes.
- This potentially involves:
  - `NativeExperiment`, in `libs/quantum_executor/base/experiment.py`: the compiled experiment that
    `to_native_experiments` returns and `run_native` runs.
  - An `instruction.py` of your own, translating the individual instructions, as each existing executor has.
- Other modules in `libs/quantum_executor` might have to be adapted or added.

### 3. Next steps
- Load your executor by its `BCC_EXECUTOR_TYPE` in `_init_executor` in `services/scheduler/utils.py`, which imports
  each executor only when it is chosen, so that the others' dependencies need not be installed.
- Add its dependencies as an extra in `pyproject.toml`, and to the Nuitka options at the top of `main.py`.
- Add documentation for your executor.
  - You probably have to update the `.env.example`, and add example configs beside `quantify-config.example.json`.
  - Provisioning, in `pkg/provision.sh`, writes an instance's example configs for its executor.
- Check whether all tests are running.

For more intuition on how the executors are integrated into the stack, check out the existing ones in the source code.
