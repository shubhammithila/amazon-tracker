"""assistant_log: questions asked of the Portfolio "Ask" assistant, with tokens and tool calls

Revision ID: e5a9c3f17d42
Revises: d2f6b8a41c07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5a9c3f17d42"
down_revision: Union[str, None] = "d2f6b8a41c07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("username", sa.String(80)),
        sa.Column("tab", sa.String(12)),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text()),
        sa.Column("tools_json", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cache_read_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("error", sa.Text()),
    )
    op.create_index("idx_assistant_log_created", "assistant_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_assistant_log_created", table_name="assistant_log")
    op.drop_table("assistant_log")
