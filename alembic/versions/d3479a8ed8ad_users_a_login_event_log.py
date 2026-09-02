"""users: a login-event log

**Additive only** — one new table, nothing existing touched. Records every login attempt from
here forward; nothing before this migration is recoverable, because nothing was being recorded.

Revision ID: d3479a8ed8ad
Revises: ff8b85879cd6
Create Date: 2026-09-02 19:27:04.384125

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3479a8ed8ad"
down_revision: Union[str, None] = "ff8b85879cd6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_login_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("via", sa.String(length=16), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_user_login_events_created", "user_login_events", ["created_at"], unique=False,
    )
    op.create_index(
        "idx_user_login_events_user", "user_login_events", ["user_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_user_login_events_user", table_name="user_login_events")
    op.drop_index("idx_user_login_events_created", table_name="user_login_events")
    op.drop_table("user_login_events")
