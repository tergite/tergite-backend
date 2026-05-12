# This code is part of Tergite
#
# (C) Axel Andersson (2022)
# (C) Martin Ahindura (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
# Refactored by Martin Ahindura (2024)

import abc
import copy
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.libs.qiskit.qobj import PulseQobjConfig, QobjExperimentHeader

T = TypeVar("T")


@dataclass(frozen=True)
class NativeExperiment(abc.ABC, Generic[T]):
    """The schema representing a single compiled experiment

    Attributes:
        header: the header to the Qobject for this experiment
        config: the Qobject config for this experiment
        schedule: the compiled schedule for this experiment
        duration: the duration of this experiment in seconds
    """

    header: QobjExperimentHeader
    config: PulseQobjConfig
    schedule: T
    duration: float


def copy_expt_header_with(header: QobjExperimentHeader, **kwargs):
    """Copies a new header from the old header with new kwargs set

    Args:
        header: the original QobjExperimentHeader header
        kwargs: the extra key-word args to set on the header

    Returns:
        a copy QobjExperimentHeader instance
    """
    new_header = copy.deepcopy(header)
    for k, v in kwargs.items():
        setattr(new_header, k, v)

    return new_header
