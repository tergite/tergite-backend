# This code is part of Tergite
#
# (C) Chalmers Next Labs 2025
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
#
"""Module the SQL store"""

from typing import List, Type

from sqlalchemy import Engine
from sqlmodel import SQLModel, create_engine


def get_sql_engine(
    url: str, models: List[Type[SQLModel]] = (), checkfirst: bool = True
) -> Engine:
    """Gets the sql engine for the given url and models

    Args:
        url: the database URL for the store
        models: the models that are in that engine
        checkfirst: Defaults to True, don't issue CREATEs for tables already present in the target database

    Returns:
        the SQLStore associated with the given URL
    """
    tables = [v.__table__ for v in models if hasattr(v, "__table__")]
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine, tables=tables, checkfirst=checkfirst)
    return engine
