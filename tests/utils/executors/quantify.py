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

from libs.quantum_executor.quantify.executor import QuantifyExecutor


class MockQuantifyExecutor(QuantifyExecutor):
    def close(self) -> None:
        """Stops the coordinator, but keeps it and the dummy clusters, which take a second
        to build, in the instruments cache for the next test's executor; the session's
        end closes them"""
        self._coordinator.stop()
