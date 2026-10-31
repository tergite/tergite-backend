# This code is part of Tergite
#
# (C) Chalmers Next Labs (2025)
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Some Typing's to simplify reading the code"""
from typing import Annotated, Dict, TypeAlias

import numpy as np
import xarray as xr

# a channel's acquisitions
QDataArray: TypeAlias = xr.DataArray
# acquisitions keyed by channel, each a QDataArray
QDataset: TypeAlias = xr.Dataset

IQValue = np.complexfloating
# a channel's acquisitions, a row per repetition and a column per acquisition
# See: https://quantify-os.org/docs/quantify-scheduler/dev/user/user_guide.html#retrieve-acquisitions-through-instrumentcoordinator
RepetitionsByAcquisitionsMatrix = Annotated[QDataArray, "repetition", "acq"]

QChannel = str
QExperimentName = str
# each channel's RepetitionsByAcquisitionsMatrix
QExperimentResult: TypeAlias = QDataset
QJobResult = Dict[QExperimentName, QExperimentResult]
