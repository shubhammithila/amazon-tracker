"""listing_sku, ads_coverage_ignore, ads_entity.asin: every SKU of a product in its SP ad group

Revision ID: d7a3e5c19b42
Revises: c5e9a2f71d36

The account's listings (every SKU with its ASIN and status, from GET_MERCHANT_LISTINGS_ALL_DATA), the
owner's "ignore this product in this ad group" marks, and the ASIN of each cached product ad, so the
Ads tab can name the SKUs an ad group is missing.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7a3e5c19b42"
down_revision: Union[str, None] = "c5e9a2f71d36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "listing_sku",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("seller_sku", sa.String(80), nullable=False),
        sa.Column("asin", sa.String(10)),
        sa.Column("status", sa.String(20)),
        sa.Column("fulfillment_channel", sa.String(20)),
        sa.Column("item_name", sa.String(500)),
        sa.Column("fetched_at", sa.DateTime),
    )
    op.create_index("idx_listing_sku_sku", "listing_sku", ["seller_sku"], unique=True)
    op.create_index("idx_listing_sku_asin", "listing_sku", ["asin"])
    op.create_table(
        "ads_coverage_ignore",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ad_group_id", sa.String(32), nullable=False),
        sa.Column("product_key", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime),
    )
    op.create_index("idx_ads_coverage_ignore", "ads_coverage_ignore",
                    ["ad_group_id", "product_key"], unique=True)
    with op.batch_alter_table("ads_entity") as batch:
        batch.add_column(sa.Column("asin", sa.String(10)))


def downgrade() -> None:
    with op.batch_alter_table("ads_entity") as batch:
        batch.drop_column("asin")
    op.drop_index("idx_ads_coverage_ignore", table_name="ads_coverage_ignore")
    op.drop_table("ads_coverage_ignore")
    op.drop_index("idx_listing_sku_asin", table_name="listing_sku")
    op.drop_index("idx_listing_sku_sku", table_name="listing_sku")
    op.drop_table("listing_sku")
