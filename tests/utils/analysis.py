# This code is part of Tergite
#
# (C) Copyright Martin Ahindura 2024
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Utilities for analysis"""
import copy
from typing import (
    Any,
    Dict,
)

import numpy as np
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis


class MockLinearDiscriminantAnalysis(LinearDiscriminantAnalysis):
    def __init__(self, result: Dict[str, Any]):
        super().__init__()
        self.__result = copy.deepcopy(result)

    def fit(self, X, y, **kwargs):
        for k, v in self.__result.items():
            setattr(self, k, v)


MOCK_LDA_1Q = MockLinearDiscriminantAnalysis(
    result={
        "intercept_": np.array([0.70658014]),
        "coef_": np.array([[-24.30078533, 22.61843697]]),
    }
)


MOCK_LDA_2Q = MockLinearDiscriminantAnalysis(
    result={
        "intercept_": np.array([-0.3046965737769689, -0.38509625914248247]),
        "coef_": np.array(
            [
                [-33.678566762457024, 20.606250322051658],
                [-37.36457774060694, 22.99757458442274],
            ]
        ),
    }
)
