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

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Type

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, col, create_engine, desc

from .exc import InvalidRequestError

# beside main.py, as in the Nuitka build
_ALEMBIC_CONFIG_FILE = Path(__file__).parents[1] / "alembic.ini"


def get_sql_engine(url: str) -> Engine:
    """Gets a new SQL engine for the given URL, which pools its connections, as every process
    makes its own engine and none forks; migrate_sql_db creates its tables"""
    engine = create_engine(str(url))
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _write_ahead)
    return engine


def migrate_sql_db(url: str) -> None:
    """Migrates the database at the URL to the models' schema, with Alembic"""
    config = Config(
        str(_ALEMBIC_CONFIG_FILE), attributes={"should_configure_logging": False}
    )
    # the config interpolates %, as a URL's escapes have it
    config.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    command.upgrade(config, "head")


@contextmanager
def open_immediate_session(db_engine: Engine) -> Iterator[Session]:
    """Opens a session that, on SQLite, takes the write lock as it begins, so that what it
    reads cannot change before it writes, even from another process"""
    with Session(db_engine) as session:
        if db_engine.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield session


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


def _write_ahead(dbapi_connection: Any, _: Any) -> None:
    """Has SQLite write ahead to a log, synced at checkpoints rather than at every commit, as
    each request's nonce commits, and lets reads go on beside a write from another process
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
