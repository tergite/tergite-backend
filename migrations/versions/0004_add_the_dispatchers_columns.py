"""add what the dispatcher claims jobs and recalibrations by

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30 07:09:59.843764

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recalibration",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("device_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("interval", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_recalibration_device_name"), "recalibration", ["device_name"]
    )
    op.create_index(op.f("ix_recalibration_due_at"), "recalibration", ["due_at"])
    op.create_index(op.f("ix_recalibration_status"), "recalibration", ["status"])

    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "stage",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(sa.Column("estimated_duration", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("created_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("executed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "is_booking_excluded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("results_file", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.create_index(batch_op.f("ix_job_created_at"), ["created_at"])
        batch_op.create_index(batch_op.f("ix_job_stage"), ["stage"])


def downgrade() -> None:
    with op.batch_alter_table("job", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_stage"))
        batch_op.drop_index(batch_op.f("ix_job_created_at"))
        batch_op.drop_column("results_file")
        batch_op.drop_column("is_booking_excluded")
        batch_op.drop_column("executed_at")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("estimated_duration")
        batch_op.drop_column("stage")

    op.drop_table("recalibration")
