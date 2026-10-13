"""fba_inventory_refresh and fba_inventory_row: FBA stock from the Inventory API

Revision ID: a7d3c9e15b20
Revises: f3b8d2a64c19

One row per read of Amazon's FBA Inventory API and one row per FBA SKU in it, so Projections can
show stock at FBA or on its way and the Shipment tab can plan against the same figure. Added 13 Oct
2026, after the Manage FBA Inventory CSV was measured missing 2,925 units of shipped stock.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3c9e15b20"
down_revision: Union[str, None] = "f3b8d2a64c19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

QUANTITIES = ("fulfillable", "inbound_working", "inbound_shipped", "inbound_receiving",
              "reserved", "researching", "future_supply", "unfulfillable")


def upgrade() -> None:
    op.create_table(
        "fba_inventory_refresh",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("status", sa.String(12), nullable=False, server_default="running"),
        sa.Column("source", sa.String(12), nullable=False, server_default="nightly"),
        sa.Column("sku_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("units", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
    )
    op.create_table(
        "fba_inventory_row",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("refresh_id", sa.Integer, nullable=False),
        sa.Column("seller_sku", sa.String(80), nullable=False),
        sa.Column("asin", sa.String(10), nullable=False),
        sa.Column("fnsku", sa.String(20)),
        *[sa.Column(q, sa.Integer, nullable=False, server_default="0") for q in QUANTITIES],
        sa.Column("amazon_updated_at", sa.String(30)),
        sa.ForeignKeyConstraint(["refresh_id"], ["fba_inventory_refresh.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_fba_inventory_row_run_sku", "fba_inventory_row", ["refresh_id", "seller_sku"],
                    unique=True)


def downgrade() -> None:
    op.drop_index("idx_fba_inventory_row_run_sku", table_name="fba_inventory_row")
    op.drop_table("fba_inventory_row")
    op.drop_table("fba_inventory_refresh")
