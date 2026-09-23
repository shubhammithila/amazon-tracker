"""portfolio economics and ads stored PER DAY, so every window is instant

Asked for as "fetching of sales and ad data should be more dynamic and updated everyday atleast
once all 7d, 30d, 60d, 90d". Keyed per WINDOW, a range nobody had fetched had no row to read, so
`GET /portfolio` returned empty and offered a ~12 minute fetch. Keyed per DAY, any sub-range inside
the coverage is a GROUP BY — the same fix `ads_performance_daily` already is for the Ads tab.

**Both granularities were measured before this migration was written**, because the code recorded
reasons NOT to use them that nobody had run:

    economics aggregateBy DAY : accepted. 7-day DAY sum == RANGE to the rupee on sales, ads, net
                                and units. 30 days = 8,010 rows in 25 s, same cost as RANGE.
    ads timeUnit DAILY        : accepted with the `date` column. 7-day DAILY cost 3,47,570.00 and
                                attributed sales 3,81,534.93 — IDENTICAL to SUMMARY, ACOS 91.10%
                                both. Amazon attributes each sale to the CLICK's day, so the
                                14-day window does not smear across day boundaries.

**The two per-window tables are DROPPED, not kept alongside.** `economics_snapshot` had reached 32
windows / 21,698 rows with no retention, one added every night — the `ads_performance` growth
problem repeating. And two caches of one figure with a read side choosing between them is precisely
the code that lost Rs 1,26,328 of Sponsored Brands spend.

`seller_sku` is `""` and NOT NULL on the new tables, unlike `economics_snapshot`: SQLite treats
NULLs as DISTINCT in a unique index, so the old nullable column never actually constrained the
ASIN-level rows and the same (window, asin) could be inserted twice.

Revision ID: e7b3f0c92a41
Revises: c5e2a91f47b3
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa

revision = "e7b3f0c92a41"
down_revision = "c5e2a91f47b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "economics_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("child_asin", sa.String(length=10), nullable=False),
        sa.Column("seller_sku", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("parent_asin", sa.String(length=10)),
        sa.Column("ordered_sales", sa.Numeric(12, 2), default=0),
        sa.Column("refunded_sales", sa.Numeric(12, 2), default=0),
        sa.Column("ad_spend", sa.Numeric(12, 2), default=0),
        sa.Column("net_proceeds", sa.Numeric(12, 2), default=0),
        sa.Column("units_ordered", sa.Integer(), default=0),
        sa.Column("units_refunded", sa.Integer(), default=0),
        sa.Column("net_units", sa.Integer(), default=0),
        sa.Column("fees_json", sa.Text()),
        sa.Column("ads_json", sa.Text()),
        sa.Column("fetched_at", sa.DateTime()),
    )
    op.create_index(
        "idx_economics_daily_day_asin_sku",
        "economics_daily",
        ["day", "child_asin", "seller_sku"],
        unique=True,
    )
    op.create_index("idx_economics_daily_day", "economics_daily", ["day"])

    op.create_table(
        "ads_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("child_asin", sa.String(length=10), nullable=False),
        sa.Column("seller_sku", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("cost", sa.Numeric(12, 2), default=0),
        sa.Column("attributed_sales", sa.Numeric(12, 2), default=0),
        sa.Column("purchases", sa.Integer(), default=0),
        sa.Column("clicks", sa.Integer(), default=0),
        sa.Column("impressions", sa.Integer(), default=0),
        sa.Column("fetched_at", sa.DateTime()),
    )
    op.create_index(
        "idx_ads_daily_pf_day_asin_sku",
        "ads_daily",
        ["day", "child_asin", "seller_sku"],
        unique=True,
    )
    op.create_index("idx_ads_daily_pf_day", "ads_daily", ["day"])

    # **Dropped, not migrated.** The stored windows cannot be split back into days — a 30-day total
    # carries no information about which day each sale fell on — so there is nothing to convert.
    # The backfill script refetches 90 days at DAY granularity instead, which is ~25 s of economics
    # plus 3 ads reports and is the only honest way to populate them.
    op.drop_table("economics_snapshot")
    op.drop_table("ads_snapshot")


def downgrade() -> None:
    # Recreated at their final shape so a downgrade leaves a working app, but EMPTY: the daily rows
    # could be summed into a window only if we knew which windows to build, and the whole point of
    # the change is that there is no longer a fixed list of them. A refresh repopulates.
    op.create_table(
        "economics_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_start", sa.String(length=10), nullable=False),
        sa.Column("window_end", sa.String(length=10), nullable=False),
        sa.Column("child_asin", sa.String(length=10), nullable=False),
        sa.Column("seller_sku", sa.String(length=80)),
        sa.Column("parent_asin", sa.String(length=10)),
        sa.Column("ordered_sales", sa.Numeric(12, 2), default=0),
        sa.Column("refunded_sales", sa.Numeric(12, 2), default=0),
        sa.Column("ad_spend", sa.Numeric(12, 2), default=0),
        sa.Column("net_proceeds", sa.Numeric(12, 2), default=0),
        sa.Column("units_ordered", sa.Integer(), default=0),
        sa.Column("units_refunded", sa.Integer(), default=0),
        sa.Column("net_units", sa.Integer(), default=0),
        sa.Column("fees_json", sa.Text()),
        sa.Column("ads_json", sa.Text()),
        sa.Column("fetched_at", sa.DateTime()),
    )
    op.create_index(
        "idx_economics_snapshot_window_asin",
        "economics_snapshot",
        ["window_start", "window_end", "child_asin", "seller_sku"],
        unique=True,
    )
    op.create_table(
        "ads_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_start", sa.String(length=10), nullable=False),
        sa.Column("window_end", sa.String(length=10), nullable=False),
        sa.Column("child_asin", sa.String(length=10), nullable=False),
        sa.Column("seller_sku", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("cost", sa.Numeric(12, 2), default=0),
        sa.Column("attributed_sales", sa.Numeric(12, 2), default=0),
        sa.Column("purchases", sa.Integer(), default=0),
        sa.Column("clicks", sa.Integer(), default=0),
        sa.Column("impressions", sa.Integer(), default=0),
        sa.Column("fetched_at", sa.DateTime()),
    )
    op.create_index(
        "idx_ads_snapshot_window_asin_sku",
        "ads_snapshot",
        ["window_start", "window_end", "child_asin", "seller_sku"],
        unique=True,
    )

    op.drop_index("idx_ads_daily_pf_day", table_name="ads_daily")
    op.drop_index("idx_ads_daily_pf_day_asin_sku", table_name="ads_daily")
    op.drop_table("ads_daily")
    op.drop_index("idx_economics_daily_day", table_name="economics_daily")
    op.drop_index("idx_economics_daily_day_asin_sku", table_name="economics_daily")
    op.drop_table("economics_daily")
