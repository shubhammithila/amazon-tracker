"""cartons are a day total, not per sku

Cartons moved from ``shipment_packing_entries.cartons`` (per day, per ASIN) to
``shipment_packing_days.total_cartons`` (per day, entered directly).

The reason is that the per-SKU question had no answer. A carton on this floor is
filled with whatever is being packed at the time, so a mixed box belongs to several
ASINs and to none of them — "carton is not item wise. it is random. like 500 units
packed today in 20 cartons". The packer was therefore guessing, and the guess summed
into the number that prefills a GST invoice's Boxes field.

**The backfill is the point of this file, not the drop.** ``total_cartons`` already
existed as a denormalised SUM of the entry column, so on any database with packing
history the two agree — but "already agrees" is a claim about code that ran in the
past, and a drop is irreversible. So the day totals are recomputed from the entries
first, and only then is the column dropped. If they had ever disagreed, the entries
were the source of truth and this preserves them.

Revision ID: 0f85fa400957
Revises: e886574dd5f5
Create Date: 2026-08-09 19:43:16.007219
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0f85fa400957"
down_revision: Union[str, None] = "e886574dd5f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Preserve the counts BEFORE dropping the column they live in. COALESCE so a day
    # with no entries becomes 0 rather than NULL — total_cartons feeds logic.is_held,
    # which would compare NULL against a threshold.
    op.execute(
        sa.text(
            """
            UPDATE shipment_packing_days
               SET total_cartons = COALESCE((
                       SELECT SUM(COALESCE(e.cartons, 0))
                         FROM shipment_packing_entries e
                        WHERE e.day_id = shipment_packing_days.id
                   ), 0)
            """
        )
    )

    # batch_alter_table because SQLite cannot DROP COLUMN before 3.35 and Alembic's
    # batch mode rebuilds the table instead. PostgreSQL ignores the batching.
    with op.batch_alter_table("shipment_packing_entries", schema=None) as batch_op:
        batch_op.drop_column("cartons")


def downgrade() -> None:
    """Restore the column, with every row at 0.

    The per-SKU split is genuinely gone: 20 cartons against a day cannot be
    apportioned across its ASINs, because the boxes were mixed. Distributing them
    pro-rata by units would invent a precise-looking number nobody recorded, so this
    leaves them at 0 and keeps the day total (which downgrade does not touch)
    authoritative. Same choice, and the same reasoning, as the legacy JSON import.
    """
    with op.batch_alter_table("shipment_packing_entries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cartons", sa.INTEGER(), nullable=True, server_default="0")
        )
