"""order_packed_entries

Units the warehouse has packed against one ASIN on one IST day, for the Orders tab's
dispatch dashboard. The only table in the Orders feature the app writes for itself —
everything else there is a cache of Amazon's data, refreshed rather than edited.

Keyed on (pack_date, asin) rather than on an order set, and that is measured rather than
stylistic: on production 200 of 264 orders flipped from `PendingPickUp` to `PickedUp`
overnight, so a tally attached to not-yet-collected orders would erase itself mid-shift the
moment the courier arrived.

`pack_date` is text, matching `shipment_packing_days.pack_date`, because the business runs in
IST while `datetime.utcnow` is 5.5 hours behind — a date derived from a UTC timestamp lands on
the wrong day for five and a half hours every night.

No data migration: the table starts empty and the screen fills it.

Revision ID: d4f9a2c68b31
Revises: c3d8e5f21a47
Create Date: 2026-08-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d4f9a2c68b31"
down_revision: Union[str, None] = "c3d8e5f21a47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_packed_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pack_date", sa.String(length=10), nullable=False),
        sa.Column("asin", sa.String(length=10), nullable=False),
        sa.Column("units", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE: a repeated save from a warehouse phone must UPDATE, not double-count — the
    # same property (day_id, asin) gives shipment packing entries.
    op.create_index(
        "idx_order_packed_date_asin",
        "order_packed_entries", ["pack_date", "asin"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_order_packed_date_asin", table_name="order_packed_entries")
    op.drop_table("order_packed_entries")
