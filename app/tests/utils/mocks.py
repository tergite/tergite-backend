# This code is part of Tergite
#
# (C) Copyright Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""Utilities for handling mocks"""

from functools import wraps
from logging import Logger
from typing import Any, Type

from pytest_mock import MockerFixture


def make_attr_verbose(
    cls: Type[Any], mock_fixture: MockerFixture, logger: Logger, attr_name: str
):
    """Makes the attribute of a given class print messages to the logger

    Args:
        cls: the class to mock patch
        mock_fixture: the mocker fixture to use for mocking
        logger: the logger to use
        attr_name: the name of the attribute to make verbose

    Returns:
         the mocked class
    """
    orig_prop = getattr(cls, attr_name)

    @wraps(orig_prop)
    def _wrapped(self, *args, **kwargs):
        logger.debug(
            "ENTER %s(is_dummy=%s) args=%r kwargs=%r",
            attr_name,
            getattr(self, "is_dummy", None),
            args,
            kwargs,
        )
        try:
            res = orig_prop(self, *args, **kwargs)
            logger.debug("EXIT  %s -> %r", attr_name, res)
            return res
        except Exception as e:
            logger.exception("EXC   %s: %s", attr_name, e)
            raise

    # install wrapper and stash the original
    mock_fixture.patch.object(cls, attr_name, _wrapped)
    setattr(cls, f"__orig_{attr_name}", orig_prop)
    return cls
