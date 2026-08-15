"""record the Amazon shipment on a packing day

Five nullable columns on ``shipment_packing_days``, so the boxes packed on a day can be
tied to the Amazon inbound shipment they went into — the shipment id that goes on the GST
invoice, plus Amazon's internal handles and the destination Amazon actually chose.

**All five are nullable with no server default, so this migration cannot lose or rewrite
data.** Every existing row simply reads NULL, meaning "we do not know which Amazon
shipment these boxes went into", which is the truth for every day packed before today.
No backfill is possible or wanted: the shipment ids for past days exist only inside
Seller Central, and inventing them would be worse than admitting we do not have them.

Why on the DAY rather than on the plan: a shipment covers a chosen SET of days, exactly
as an invoice does, and ``invoice_id`` already lives here for the same reason. Putting it
on the plan would make one shipment per week the only expressible shape.

Why ``destination_warehouse_id`` is separate from the FC the owner picks: his pick is a
REQUEST, and Amazon's answer is what happened. They can differ, and the destination state
decides which of the 15 GSTINs applies — so conflating them would put the wrong state's
GSTIN on a tax document with nothing able to detect it.

Revision ID: 7c1a4e9b2d38
Revises: 394fc6f28429
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7c1a4e9b2d38"
down_revision: Union[str, None] = "394fc6f28429"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table because SQLite rewrites the table for an ALTER; PostgreSQL
    # ignores the batching. Adding nullable columns would work either way, but the
    # batch form keeps this consistent with the migrations around it.
    with op.batch_alter_table("shipment_packing_days", schema=None) as batch_op:
        batch_op.add_column(sa.Column("inbound_plan_id", sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column("amazon_shipment_id", sa.String(length=60), nullable=True))
        batch_op.add_column(
            sa.Column("shipment_confirmation_id", sa.String(length=30), nullable=True)
        )
        batch_op.add_column(
            sa.Column("destination_warehouse_id", sa.String(length=10), nullable=True)
        )
        batch_op.add_column(sa.Column("destination_state", sa.String(length=50), nullable=True))


def downgrade() -> None:
    """Drops the five columns.

    Genuinely lossy, and stated rather than glossed: the Amazon shipment id for a day
    exists nowhere else in this app, so a downgrade forgets which shipment covered which
    boxes. Recoverable only from Seller Central by hand.
    """
    with op.batch_alter_table("shipment_packing_days", schema=None) as batch_op:
        batch_op.drop_column("destination_state")
        batch_op.drop_column("destination_warehouse_id")
        batch_op.drop_column("shipment_confirmation_id")
        batch_op.drop_column("amazon_shipment_id")
        batch_op.drop_column("inbound_plan_id")
