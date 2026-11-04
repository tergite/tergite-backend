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
"""Fixtures that spy on the connections the app opens"""

import pytest


@pytest.fixture
def mss_conn_spy(mocker, mock_mss):
    """Spies on calls to websockets.sync.client.connect (as imported in mss.service)."""
    return mocker.patch(
        "services.external.mss.service.connect", side_effect=mock_mss.connect
    )


@pytest.fixture
def sql_engine_spy(mocker):
    """Spies on calls to create_engine (as imported in utils.sql_db)."""
    from sqlalchemy import create_engine

    return mocker.patch("utils.sql_db.create_engine", wraps=create_engine)
