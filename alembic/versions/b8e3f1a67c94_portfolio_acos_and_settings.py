"""portfolio acos and settings

Adds true ACOS to the Portfolio tab, plus editable verdict thresholds.

ads_snapshot        — ad cost against ATTRIBUTED sales per (window, asin, seller_sku), from the
                      Advertising API. A separate table from economics_snapshot because it comes
                      from a separate API that takes ~12 minutes rather than 30 seconds, so an
                      ads failure must not be able to cost the margins.
portfolio_settings  — the verdict thresholds as one JSON row, so a new rule needs no migration.
economics_snapshot  — gains a nullable `seller_sku`, and the unique index gains it too.

Why `seller_sku` on the existing table rather than a third one: 186 of 267 child ASINs sell under
both a merchant/Easy Ship SKU and an identically-named "… FBA" SKU. The dashboard shows them
COMBINED (which the CHILD_ASIN aggregation already does); the per-SKU rows exist only to show the
split on expand. They live beside the ASIN-level rows with the same columns, so a second table
would mean two places that know what an economics row looks like.

**The ASIN-level rows keep `seller_sku` NULL**, so every existing row stays valid and
`load_snapshot` filters on `seller_sku IS NULL` to keep totals authoritative.

`batch_alter_table` is required for the SQLite index swap (SQLite cannot drop a column or alter an
index in place); it is a documented no-op on PostgreSQL.

No data migration: the new tables start empty, and existing economics rows are already the
ASIN-level grain this expects.

Revision ID: b8e3f1a67c94
Revises: a7c4e91b58d2
Create Date: 2026-08-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b8e3f1a67c94"
down_revision: Union[str, None] = "a7c4e91b58d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ads_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(length=10), nullable=False),
        sa.Column("window_end", sa.String(length=10), nullable=False),
        sa.Column("child_asin", sa.String(length=10), nullable=False),
        sa.Column("seller_sku", sa.String(length=80), nullable=False,
                  server_default=sa.text("''")),
        sa.Column("cost", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("attributed_sales", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("purchases", sa.Integer(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE: re-running a refresh for the same window must UPDATE, never double the ad spend.
    op.create_index(
        "idx_ads_snapshot_window_asin_sku",
        "ads_snapshot",
        ["window_start", "window_end", "child_asin", "seller_sku"],
        unique=True,
    )

    op.create_table(
        "portfolio_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_portfolio_settings_name", "portfolio_settings", ["name"], unique=True
    )

    # The per-SKU breakdown column, plus the widened unique index. Batch mode because SQLite
    # cannot alter an index in place.
    with op.batch_alter_table("economics_snapshot") as batch:
        batch.add_column(sa.Column("seller_sku", sa.String(length=80), nullable=True))
    op.drop_index("idx_economics_snapshot_window_asin", table_name="economics_snapshot")
    op.create_index(
        "idx_economics_snapshot_window_asin",
        "economics_snapshot",
        ["window_start", "window_end", "child_asin", "seller_sku"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_economics_snapshot_window_asin", table_name="economics_snapshot")
    op.create_index(
        "idx_economics_snapshot_window_asin",
        "economics_snapshot",
        ["window_start", "window_end", "child_asin"],
        unique=True,
    )
    with op.batch_alter_table("economics_snapshot") as batch:
        batch.drop_column("seller_sku")

    op.drop_index("idx_portfolio_settings_name", table_name="portfolio_settings")
    op.drop_table("portfolio_settings")
    op.drop_index("idx_ads_snapshot_window_asin_sku", table_name="ads_snapshot")
    op.drop_table("ads_snapshot")
