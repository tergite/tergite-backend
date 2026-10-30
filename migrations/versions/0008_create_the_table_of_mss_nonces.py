"""create the table of MSS's nonces, which were in Redis

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-30 13:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mss_nonce",
        sa.Column("device_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("nonce", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("device_name", "nonce"),
    )
    op.create_index(op.f("ix_mss_nonce_expires_at"), "mss_nonce", ["expires_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_mss_nonce_expires_at"), table_name="mss_nonce")
    op.drop_table("mss_nonce")
