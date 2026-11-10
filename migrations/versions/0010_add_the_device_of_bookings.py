"""add the device of bookings

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-10 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("booking", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("device_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
    _give_bookings_to_the_migrating_device()
    with op.batch_alter_table("booking", schema=None) as batch_op:
        batch_op.alter_column(
            "device_name",
            existing_type=sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        )
        batch_op.create_index(batch_op.f("ix_booking_device_name"), ["device_name"])


def downgrade() -> None:
    with op.batch_alter_table("booking", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_booking_device_name"))
        batch_op.drop_column("device_name")


def _give_bookings_to_the_migrating_device() -> None:
    """Gives the bookings made before they were each a device's to the device migrating
    the database, which is theirs unless devices shared it"""
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM booking")).first() is None:
        return
    connection.execute(
        sa.text("UPDATE booking SET device_name = :device_name"),
        {"device_name": _get_migrating_device_name()},
    )


def _get_migrating_device_name() -> str:
    """Gets the device that migrate_sql_db names, or, run by hand, the env file's"""
    device_name = context.config.attributes.get("device_name")
    if device_name is None:
        # imported only here, as reading it needs the device's whole env file
        import settings

        device_name = settings.get_device_settings().default_prefix
    return device_name
