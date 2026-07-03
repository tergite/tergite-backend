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

from typing import Dict, Iterable, List, Type

from sqlalchemy import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel, col, create_engine, desc

from .exc import InvalidRequestError

_ENGINE_CACHE: Dict[str, Engine] = {}


def clear_sql_engine_cache() -> None:
    """Clears the SQL engine cache"""
    _ENGINE_CACHE.clear()


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
    url = str(url)
    engine = _ENGINE_CACHE.get(url)
    if engine is None:
        tables = [v.__table__ for v in models if hasattr(v, "__table__")]
        kwargs = {}
        if url.startswith("sqlite"):
            # to avoid prefork-errors with sqlite since we are running in multiple processes,
            # we use NullPool
            kwargs["poolclass"] = NullPool

        engine = create_engine(url, **kwargs)
        SQLModel.metadata.create_all(engine, tables=tables, checkfirst=checkfirst)
        _ENGINE_CACHE[url] = engine
    return engine


def convert_http_sort_to_db_sort(model: Type[SQLModel], http_sort: Iterable[str] = ()):
    """Coverts an HTTP sort list comprising strings into a sort tuple for the database

    Args:
        model: the SQLModel to handle to sort through
        http_sort: a list of strings; prepending a "-" is treated as descending order

    Returns:
        the native sort tuple that can be passed to sqlalchemy.order_by()

    Raises:
        InvalidRequestError: field does exist
    """
    return tuple(
        (
            desc(_get_field(model, v.lstrip("-")))
            if v.startswith("-")
            else col(_get_field(model, v))
        )
        for v in http_sort
    )


def _get_field(model: Type[SQLModel], field: str):
    """Gets the field from the given model

    Args:
        model: the SQLModel from which to get the field.
        field: the property to get from the model

    Returns:
        the field of the given model

    Raises:
        InvalidRequestError: field {field} does exist
    """
    try:
        return getattr(model, field)
    except AttributeError as exp:
        raise InvalidRequestError(f"field {field} does not exist") from exp
