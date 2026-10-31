# This code is part of Tergite
#
# (C) Axel Andersson (2022)
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

from typing import Any, List, Tuple, Union


def iqx_rld(enc_seq: List[Union[Tuple[Any], Tuple[Any, int]]]) -> List[Any]:
    """
    Decodes a run-length encoded sequence. Omitted counts are interpreted as 1.
    """
    dec = [[t[0] for _ in range(t[1])] if len(t) == 2 else [t[0]] for t in enc_seq]
    return [item for sublist in dec for item in sublist]
