"""closed_at on a plan, carried_from_plan_id on a packing day

Two nullable columns, no backfill, so every pre-existing row reads "never closed" and
"never carried" — which is true of all of them.

`carried_from_plan_id` is intentionally NOT a foreign key. The source plan can be
deleted (the plan delete cascades to its items, days and entries), and a FK would
either refuse that delete or null this column out. The value's only purpose is to
explain why a plan holds units for a date it never opened, and a stale id still does
that; a NULL does not.

Revision ID: b2f7c1a94e05
Revises: 9e4b1c7a2f56
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b2f7c1a94e05"
down_revision: Union[str, None] = "9e4b1c7a2f56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table because SQLite rewrites the table for an ALTER; PostgreSQL
    # ignores the batching. Consistent with the migrations around it.
    with op.batch_alter_table("shipment_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("closed_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("shipment_packing_days", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("carried_from_plan_id", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("shipment_packing_days", schema=None) as batch_op:
        batch_op.drop_column("carried_from_plan_id")
    with op.batch_alter_table("shipment_plans", schema=None) as batch_op:
        batch_op.drop_column("closed_at")
