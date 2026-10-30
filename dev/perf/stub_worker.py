# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs AB 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Runs main.py with the qiskit executor's simulation stubbed out, so that the performance
baselines measure the scheduler and not the physics; dev/perf/record.sh runs the heavy worker
with it

The qiskit executor is swapped before main.py builds one, so BCC_ENV_FILE must name the
device's env file: settings is read on import, before main.py could set it.
"""

if __name__ == "__main__":
    # not at the top: RQ's scheduler runs in a spawned process that re-imports this module,
    # and would load the whole qiskit stack for nothing
    import app.libs.quantum_executor.qiskit.executor as qiskit_executor
    import main
    from dev.perf.stub_executor import StubExecutor

    qiskit_executor.QiskitDynamicsExecutor = StubExecutor
    main.main()
