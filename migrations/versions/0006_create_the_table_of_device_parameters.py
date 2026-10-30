"""create the table of device parameters, which were in Redis

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_parameters",
        sa.Column("backend_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("device_info", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "calibration_info", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.PrimaryKeyConstraint("backend_name"),
    )


def downgrade() -> None:
    op.drop_table("device_parameters")
