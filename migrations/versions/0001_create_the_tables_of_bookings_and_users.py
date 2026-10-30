"""create the tables of bookings and users

Revision ID: 0001
Revises:
Create Date: 2026-10-30 06:27:01.385240

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # a database from before Alembic has them already, as SQLModel created them
    if sa.inspect(op.get_bind()).has_table("user"):
        return

    op.create_table(
        "user",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("email", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("password", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_email"), "user", ["email"], unique=True)
    op.create_index(op.f("ix_user_id"), "user", ["id"], unique=False)

    op.create_table(
        "booking",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("start_utc", sa.DateTime(), nullable=False),
        sa.Column("end_utc", sa.DateTime(), nullable=False),
        sa.Column("start_event_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("end_event_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("idle_timer_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("total_duration", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_booking_end_utc"), "booking", ["end_utc"], unique=False)
    op.create_index(op.f("ix_booking_id"), "booking", ["id"], unique=False)
    op.create_index(
        op.f("ix_booking_start_utc"), "booking", ["start_utc"], unique=False
    )
    op.create_index(op.f("ix_booking_user_id"), "booking", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("booking")
    op.drop_table("user")
