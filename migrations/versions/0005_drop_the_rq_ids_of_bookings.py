"""drop the rq ids of bookings

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30 09:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("start_event_id", "end_event_id", "idle_timer_id")


def upgrade() -> None:
    with op.batch_alter_table("booking", schema=None) as batch_op:
        for column in _COLUMNS:
            batch_op.drop_column(column)


def downgrade() -> None:
    with op.batch_alter_table("booking", schema=None) as batch_op:
        for column in _COLUMNS:
            batch_op.add_column(
                sa.Column(
                    column,
                    sqlmodel.sql.sqltypes.AutoString(),
                    nullable=False,
                    server_default="",
                )
            )
