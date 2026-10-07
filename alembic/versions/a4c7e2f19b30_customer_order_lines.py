"""customer_order_lines + repeat_refresh (Portfolio -> Repeat customers)

Revision ID: a4c7e2f19b30
Revises: c3d8e1f5a702
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4c7e2f19b30"
down_revision: Union[str, None] = "c3d8e1f5a702"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customer_order_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("amazon_order_id", sa.String(30), nullable=False),
        sa.Column("shipment_item_id", sa.String(30), nullable=False),
        sa.Column("buyer_key", sa.String(32), nullable=False),
        sa.Column("purchase_day", sa.String(10), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False, server_default="fba"),
        sa.Column("seller_sku", sa.String(80), nullable=False, server_default=""),
        sa.Column("child_asin", sa.String(10)),
        sa.Column("parent_asin", sa.String(10)),
        sa.Column("units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime()),
    )
    op.create_index("idx_customer_order_lines_item", "customer_order_lines",
                    ["amazon_order_id", "shipment_item_id"], unique=True)
    op.create_index("idx_customer_order_lines_day", "customer_order_lines", ["purchase_day"])
    op.create_table(
        "repeat_refresh",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_start", sa.String(10)),
        sa.Column("window_end", sa.String(10)),
        sa.Column("status", sa.String(12), nullable=False, server_default="done"),
        sa.Column("rows_seen", sa.Integer()),
        sa.Column("lines_stored", sa.Integer()),
        sa.Column("skipped_no_date", sa.Integer()),
        sa.Column("skipped_zero_price", sa.Integer()),
        sa.Column("skipped_no_key", sa.Integer()),
        sa.Column("unresolved_sku", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("repeat_refresh")
    op.drop_index("idx_customer_order_lines_day", table_name="customer_order_lines")
    op.drop_index("idx_customer_order_lines_item", table_name="customer_order_lines")
    op.drop_table("customer_order_lines")
