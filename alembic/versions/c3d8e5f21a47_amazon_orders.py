"""amazon_orders and amazon_order_items

A local cache of Amazon Easy Ship orders, so the Orders tab can render instantly.
`getOrders` is rate-limited to 0.045 req/sec (one call every 22 seconds, measured on the
live account), so fetching per request is not an option — the page would hang and two
viewers would 429.

No data migration: the tables start empty and the background refresh fills them.

Revision ID: c3d8e5f21a47
Revises: b2f7c1a94e05
Create Date: 2026-08-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c3d8e5f21a47"
down_revision: Union[str, None] = "b2f7c1a94e05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "amazon_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("amazon_order_id", sa.String(length=20), nullable=False),
        sa.Column("purchase_date_utc", sa.DateTime(), nullable=True),
        sa.Column("latest_ship_date_utc", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("easyship_status", sa.String(length=30), nullable=True),
        sa.Column("ship_service_level", sa.String(length=60), nullable=True),
        sa.Column("order_total", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=5), nullable=True),
        sa.Column("items_ordered", sa.Integer(), nullable=True),
        sa.Column("items_shipped", sa.Integer(), nullable=True),
        sa.Column("is_prime", sa.Boolean(), nullable=True),
        sa.Column("is_cod", sa.Boolean(), nullable=True),
        sa.Column("city", sa.String(length=60), nullable=True),
        sa.Column("state", sa.String(length=60), nullable=True),
        sa.Column("postal_code", sa.String(length=12), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("items_fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE: the upsert target. A repeated refresh must update, not duplicate — the same
    # property the (plan_id, pack_date) index gives packing days.
    op.create_index(
        op.f("ix_amazon_orders_amazon_order_id"),
        "amazon_orders", ["amazon_order_id"], unique=True,
    )
    op.create_table(
        "amazon_order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("asin", sa.String(length=10), nullable=False),
        sa.Column("seller_sku", sa.String(length=80), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("quantity_ordered", sa.Integer(), nullable=True),
        sa.Column("quantity_shipped", sa.Integer(), nullable=True),
        sa.Column("item_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("item_tax", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("promotion_discount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["amazon_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_amazon_order_items_order_asin",
        "amazon_order_items", ["order_id", "asin"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_amazon_order_items_order_asin", table_name="amazon_order_items")
    op.drop_table("amazon_order_items")
    op.drop_index(op.f("ix_amazon_orders_amazon_order_id"), table_name="amazon_orders")
    op.drop_table("amazon_orders")
