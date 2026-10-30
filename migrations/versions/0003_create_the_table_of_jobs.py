"""create the table of jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30 06:39:46.742315

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("device_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("job_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("data", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("device_name", "job_id"),
    )
    op.create_index(op.f("ix_job_status"), "job", ["status"], unique=False)
    op.create_index(op.f("ix_job_updated_at"), "job", ["updated_at"], unique=False)
    op.create_index(op.f("ix_job_user_id"), "job", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("job")
