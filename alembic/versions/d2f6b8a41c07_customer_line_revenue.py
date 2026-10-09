"""customer_order_lines.revenue: what the customer paid for the line (Customer value -> LTV)

Revision ID: d2f6b8a41c07
Revises: a4c7e2f19b30

NULL, never 0, on every pre-existing row: those lines were stored before the price was read, and
"not fetched yet" must not read as "free". The backfill re-reads them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2f6b8a41c07"
down_revision: Union[str, None] = "a4c7e2f19b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("customer_order_lines") as batch:
        batch.add_column(sa.Column("revenue", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customer_order_lines") as batch:
        batch.drop_column("revenue")
