"""ads_product_daily, ads_rule.level, ads_refresh.product_*: rules that turn a SKU off

Revision ID: c5e9a2f71d36
Revises: b8e4d1f27c60

Per-day spend and sales per Sponsored Products product ad (one SKU in one ad group), so a rule can
turn a single SKU off or on. Kept apart from ads_performance_daily, whose keyword rows are the same
money seen from the other side.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5e9a2f71d36"
down_revision: Union[str, None] = "b8e4d1f27c60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ads_product_daily",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("ad_id", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(32)),
        sa.Column("ad_group_id", sa.String(32)),
        sa.Column("asin", sa.String(10)),
        sa.Column("sku", sa.String(80)),
        sa.Column("impressions", sa.Integer),
        sa.Column("clicks", sa.Integer),
        sa.Column("spend", sa.Numeric(12, 2)),
        sa.Column("orders", sa.Integer),
        sa.Column("sales", sa.Numeric(12, 2)),
        sa.Column("fetched_at", sa.DateTime),
    )
    op.create_index("idx_ads_product_daily_day_ad", "ads_product_daily", ["day", "ad_id"], unique=True)
    op.create_index("idx_ads_product_daily_day_group", "ads_product_daily", ["day", "ad_group_id"])
    with op.batch_alter_table("ads_rule") as batch:
        batch.add_column(sa.Column("level", sa.String(10), nullable=False, server_default="keyword"))
    with op.batch_alter_table("ads_refresh") as batch:
        batch.add_column(sa.Column("product_rows", sa.Integer, server_default="0"))
        batch.add_column(sa.Column("product_error", sa.Text))


def downgrade() -> None:
    with op.batch_alter_table("ads_refresh") as batch:
        batch.drop_column("product_error")
        batch.drop_column("product_rows")
    with op.batch_alter_table("ads_rule") as batch:
        batch.drop_column("level")
    op.drop_index("idx_ads_product_daily_day_group", table_name="ads_product_daily")
    op.drop_index("idx_ads_product_daily_day_ad", table_name="ads_product_daily")
    op.drop_table("ads_product_daily")
