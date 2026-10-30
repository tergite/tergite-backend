"""create the table of switched off devices, whose flags were in Redis

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30 12:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "switched_off_device",
        sa.Column("device_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("device_name"),
    )


def downgrade() -> None:
    op.drop_table("switched_off_device")
