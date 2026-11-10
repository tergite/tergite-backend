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
import pytest
from alembic import command
from sqlalchemy import inspect

import settings
from api.worker import preload_worker_globals
from services.booking import get_many_bookings, get_user_profile
from services.scheduler import close_device_resources
from tests.utils.params import FASTAPI_CLIENTS
from utils.sql_db import get_alembic_config, get_sql_engine, migrate_sql_db


def test_the_migrations_match_the_models(tmp_path):
    """Fails when a model changed without a migration: `alembic revision --autogenerate`"""
    config = get_alembic_config(f"sqlite:///{tmp_path}/bcc.db")

    command.upgrade(config, "head")
    command.check(config)


def test_a_database_from_before_alembic_is_upgraded_with_its_rows(tmp_path):
    url = f"sqlite:///{tmp_path}/bcc.db"
    command.upgrade(get_alembic_config(url), "0001")
    db_engine = get_sql_engine(url)
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO user (id, name, email, is_admin, password) "
            "VALUES ('u1', 'Ada', 'ada@example.com', 0, '')"
        )
        connection.exec_driver_sql("DROP TABLE alembic_version")

    migrate_sql_db(url)

    user_columns = {column["name"] for column in inspect(db_engine).get_columns("user")}
    assert "password" not in user_columns
    assert get_user_profile(db_engine, "u1").email == "ada@example.com"


def test_bookings_from_before_they_were_keyed_by_device_go_to_the_migrating_one(
    tmp_path,
):
    url = f"sqlite:///{tmp_path}/bcc.db"
    command.upgrade(get_alembic_config(url), "0009")
    db_engine = get_sql_engine(url)
    with db_engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO booking (id, user_id, start_utc, end_utc, total_duration) "
            "VALUES ('b1', NULL, '2026-11-10 10:00:00', '2026-11-10 11:00:00', 3600)"
        )

    migrate_sql_db(url, device_name="alpha")

    assert [booking.id for booking in get_many_bookings(db_engine, "alpha")] == ["b1"]


@pytest.mark.parametrize("client", FASTAPI_CLIENTS)
def test_a_worker_migrates_the_database_it_starts_on(client, tmp_path):
    """The client loads its device's settings, which the worker starts with"""
    url = f"sqlite:///{tmp_path}/bcc.db"
    device_settings = settings.get_device_settings().model_copy(
        update={"booking_db_url": url, "storage_root": tmp_path}
    )

    device = preload_worker_globals(device_settings, should_load_executor=False)
    close_device_resources(device)

    assert "job" in inspect(get_sql_engine(url)).get_table_names()
