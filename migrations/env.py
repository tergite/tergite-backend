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
"""Alembic's environment: the tables of the models, in a device's SQL database

The API migrates each device's database as it starts. By hand, run from the repository root with
the device's env file in BCC_ENV_FILE: `alembic upgrade head`, or `alembic revision --autogenerate
-m "..."` against an up-to-date database after a model changes. Review a generated revision by
hand before committing it.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from app.services.booking import Booking
from app.services.devices.store import DeviceParametersRow
from app.services.scheduler.store import JobRow, RecalibrationRow

# the models whose tables Alembic migrates, User's coming with Booking's module
_MODELS = (Booking, DeviceParametersRow, JobRow, RecalibrationRow)
target_metadata = SQLModel.metadata
# a URL set in Alembic's config, as the API and the tests set one, stands in for the env file's
database_url = context.config.get_main_option("sqlalchemy.url")
if database_url is None:
    # imported only here, as reading it needs the device's whole env file
    import settings

    database_url = settings.get_device_settings().booking_db_url

# not when run in-process, by the API, whose logging this would reset
if context.config.attributes.get("should_configure_logging", True):
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)


def run_migrations_offline() -> None:
    """Writes the migrations as SQL instead of running them"""
    context.configure(
        url=database_url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url, poolclass=NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite alters a table only by copying it
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
