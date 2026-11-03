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
"""tergite-tuner on a simulated chip: it calibrates the chip as it would real hardware,
in its real cluster mode, its cluster a dummy with the chip behind it

tergite-tuner's dummy cluster mode replaces each node's acquisitions with the node's own
fakes, so the tuner runs in its real mode, connecting through the Cluster and
ClusterComponent that its backend/hardware.py imports, which this swaps out.
"""
from contextlib import contextmanager
from functools import partial
from typing import Any, Dict, Iterator
from unittest import mock

from qblox_instruments import Cluster, ClusterType
from tergite_tuner.backend import hardware

from .component import SimulatingClusterComponent
from .engine import SimulatedChip


@contextmanager
def connect_tuner_to_chip(
    modules: Dict[int, ClusterType], chip: SimulatedChip
) -> Iterator[None]:
    """While in the context, tergite-tuner's real cluster is a dummy of the modules, its
    coordinator's component playing each schedule on the chip

    Args:
        modules: the dummy cluster's modules, by slot
        chip: the chip behind it

    Yields:
        nothing; the tuner connects to the chip until the context exits

    Raises:
        AttributeError: tergite-tuner's backend/hardware.py no longer connects through
            a Cluster and ClusterComponent of its own
    """

    class ChipCluster(Cluster):
        """A dummy cluster of the modules, however the tuner addresses it"""

        def __init__(self, name: str, *args: Any, **kwargs: Any) -> None:
            super().__init__(name, dummy_cfg=modules)

    component = partial(SimulatingClusterComponent, chip=chip)
    with mock.patch.object(hardware, "Cluster", ChipCluster):
        with mock.patch.object(hardware, "ClusterComponent", component):
            yield
