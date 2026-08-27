"""order_packed_state

A per-ORDER packed tick for the Orders tab, so the floor can mark a parcel finished and nobody
hands over a half-packed one.

Why a second table rather than a column on order_packed_entries: that table is keyed
(pack_date, asin) and counts units per SKU, which cannot express "is THIS order complete". An
order holding two different products contributes to two ASIN rows and neither knows the parcel
is unfinished. Measured on 2026-08-27: 85 orders produced 86 item lines.

A boolean tick, not a status lifecycle. "Handed to the courier" is already Amazon's
easyship_status (PickedUp), and duplicating it locally would be a second source of truth.

The order id is TEXT, not an FK to amazon_orders.id: it is what a barcode carries (so a scanner
can look it up directly), and amazon_orders is a cache that retention deletes from — an FK would
either block the purge or cascade the warehouse's own record away with it.

No data migration: the table starts empty, absence means "not packed", and un-ticking deletes
the row.

Revision ID: f6b2d4907ae3
Revises: e5a1b83c26df
Create Date: 2026-08-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f6b2d4907ae3"
down_revision: Union[str, None] = "e5a1b83c26df"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_packed_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pack_date", sa.String(length=10), nullable=False),
        sa.Column("amazon_order_id", sa.String(length=20), nullable=False),
        sa.Column("packed_at", sa.DateTime(), nullable=True),
        sa.Column("packed_by", sa.String(length=50), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE on the pair: a repeated tick from a warehouse phone that lost its response must
    # UPDATE one row, never store the same order twice for the same day. Created as a separate
    # index rather than inline, matching every other table here.
    op.create_index(
        "idx_order_packed_state_date_order",
        "order_packed_state",
        ["pack_date", "amazon_order_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_order_packed_state_date_order", table_name="order_packed_state")
    op.drop_table("order_packed_state")
