"""create the table of the events kept for MSS until it can be reached

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30 21:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mss_event",
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("device_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("data", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("id"),
    )
    op.create_index(op.f("ix_mss_event_device_name"), "mss_event", ["device_name"])


def downgrade() -> None:
    op.drop_index(op.f("ix_mss_event_device_name"), table_name="mss_event")
    op.drop_table("mss_event")
