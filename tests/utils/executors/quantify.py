# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025, 2026
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
from typing import Collection, Dict

import numpy as np
import toml
from pytest_mock import MockerFixture
from quantify_scheduler import Schedule

from libs.quantum_executor.quantify.executor import QuantifyExecutor
from tests.utils.env import TEST_QUANTIFY_SEED_FILE


class MockQuantifyExecutor(QuantifyExecutor):
    def close(self) -> None:
        """Stops the coordinator, but keeps it and the dummy clusters, which take a second
        to build, in the instruments cache for the next test's executor; the session's
        end closes them"""
        self._coordinator.stop()


def use_excited_transmons(mocker: MockerFixture, excited: Collection[str]) -> None:
    """Makes the quantify executor's gate-level runs, on the dummy clusters, which acquire
    nothing meaningful, acquire the given transmons in state 1 and the others in state 0,
    each channel by the transmon its schedule measures on it

    Args:
        mocker: the mocker that patches the executor
        excited: the names of the transmons acquired in state 1, e.g. q12
    """
    run_native = MockQuantifyExecutor.run_native

    def run_native_with_states(self, experiment, *, native_config, logger):
        dataset = run_native(
            self, experiment, native_config=native_config, logger=logger
        )
        for channel, element in _get_measured_transmons(experiment.schedule).items():
            point = get_iq_point(element, state=int(element in excited))
            key = channel if channel in dataset else str(channel)
            dataset[key] = dataset[key].copy(data=np.full_like(dataset[key], point))
        return dataset

    mocker.patch.object(MockQuantifyExecutor, "run_native", run_native_with_states)


def get_iq_point(transmon: str, state: int) -> complex:
    """Gets an IQ point on the real axis that the test seed's linear discriminator of the
    transmon classifies as the state

    Args:
        transmon: the name of the transmon, e.g. q12
        state: 0 or 1

    Returns:
        the IQ point
    """
    seed = toml.load(TEST_QUANTIFY_SEED_FILE)
    lda = seed["calibration_config"]["discriminators"]["lda"][transmon]
    margin = 1.0 if state else -1.0
    return complex((margin - lda["intercept"]) / lda["coef_0"], 0.0)


def _get_measured_transmons(schedule: Schedule) -> Dict[int, str]:
    """Gets the transmon each acquisition channel of the gate-level schedule measures"""
    return {
        operation.data["gate_info"]["acq_channel_override"]: operation.data[
            "gate_info"
        ]["device_elements"][0]
        for operation in schedule.operations.values()
        if operation.data["gate_info"].get("operation_type") == "measure"
    }
