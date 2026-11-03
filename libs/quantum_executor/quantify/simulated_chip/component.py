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
"""The component of a dummy cluster whose acquisitions are a simulated chip's"""
from typing import Any, Dict, List

from qblox_instruments import Cluster, DummyBinnedAcquisitionData
from quantify_scheduler.enums import BinMode
from quantify_scheduler.instrument_coordinator import InstrumentCoordinator
from quantify_scheduler.instrument_coordinator.components.qblox import (
    ClusterComponent,
)
from quantify_scheduler.schedules.schedule import CompiledSchedule

from .engine import AcquiredChannel, SimulatedChip


class SimulatingClusterComponent(ClusterComponent):
    """A dummy cluster's component that, as it is prepared for a schedule, plays the
    schedule on a simulated chip and loads the cluster's bins with the chip's
    acquisitions, so that the coordinator's start and retrieve_acquisition return them,
    through quantify's whole path"""

    def __new__(
        cls, instrument: Cluster, chip: SimulatedChip
    ) -> "SimulatingClusterComponent":
        """Keeps the instance from the garbage collector, as quantify's components do,
        whose constructor takes the instrument alone"""
        return super().__new__(cls, instrument)

    def __init__(self, instrument: Cluster, chip: SimulatedChip) -> None:
        """
        Args:
            instrument: the dummy cluster
            chip: the chip its schedules play on
        """
        super().__init__(instrument)
        self.chip = chip

    def prepare(self, options: Dict[str, Any]) -> None:
        """Prepares the cluster for a schedule, as quantify's coordinator does, and loads
        its bins with what the chip acquires in the schedule

        Args:
            options: the schedule's compiled instructions for the cluster

        Raises:
            LookupError: no instrument coordinator is preparing the schedule of these
                instructions
            ValueError: the simulated chip plays no {operation}, or another schedule it
                cannot play
        """
        super().prepare(options)
        acquired = self.chip.play(_get_prepared_schedule(self.instrument.name, options))
        for name, module in options.items():
            if isinstance(module, dict) and "acq_metadata" in module:
                _load_module(self.instrument, name, module, acquired)


def _get_prepared_schedule(cluster: str, options: Dict[str, Any]) -> CompiledSchedule:
    """Gets the schedule an instrument coordinator is preparing, whose compiled
    instructions for the cluster are these options: a coordinator keeps the schedule it
    prepares before it prepares its components

    Raises:
        LookupError: no instrument coordinator is preparing the schedule of these
            instructions
    """
    for coordinator in InstrumentCoordinator.instances():
        try:
            schedule = coordinator.last_schedule
        except ValueError:
            continue
        if schedule["compiled_instructions"].get(cluster) is options:
            return schedule
    raise LookupError(
        f"no instrument coordinator is preparing the schedule of {cluster}'s instructions"
    )


def _load_module(
    cluster: Cluster,
    name: str,
    module: Dict[str, Any],
    acquired: Dict[int, AcquiredChannel],
) -> None:
    """Loads each sequencer of the module with the bins of its acquisition channels, each
    bin the integral the clusters return: the mean IQ point times the integration's
    samples"""
    slot = int(name.rsplit("module", 1)[1])
    for sequencer, metadata in module["acq_metadata"].items():
        index = int(sequencer[len("seq") :])
        length = module["sequencers"][sequencer].integration_length_acq
        cluster.delete_dummy_binned_acquisition_data(slot, index)
        for qblox_index, channel in metadata.acq_channels_metadata.items():
            bins = _get_bins(
                acquired[channel.acq_channel],
                channel.acq_indices,
                length,
                metadata.repetitions,
            )
            cluster.set_dummy_binned_acquisition_data(
                slot, index, str(qblox_index), bins
            )


def _get_bins(
    acquired: AcquiredChannel,
    indices: List[int],
    length: int,
    repetitions: int,
) -> List[DummyBinnedAcquisitionData]:
    """Gets the bins of an acquisition channel: an averaging one's in the order of its
    acquisition indices, an appending one's in the order its shots ran"""
    if acquired.bin_mode == BinMode.AVERAGE:
        means = dict(zip(acquired.indices, acquired.values))
        values = [means[index] for index in sorted(indices)]
        count = repetitions
    else:
        values, count = acquired.values, 1
    return [
        DummyBinnedAcquisitionData(
            data=(value.real * length, value.imag * length), thres=0, avg_cnt=count
        )
        for value in values
    ]
