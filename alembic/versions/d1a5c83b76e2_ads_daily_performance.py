"""ads: per-day performance rows, so any date range is instant

`ads_performance` holds one row per entity per WINDOW, so a range nobody fetched has no row to read
— which is why picking 20 days out of a cached 30 needed a fresh ~6-minute Amazon report. Daily rows
are summable: any range inside what we hold becomes a GROUP BY with no Amazon call at all.

Measured before choosing this over prefetching a few fixed presets:

    DAILY report, 7 days        45,650 rows   (SUMMARY: 12,854)
    bulk insert throughput      30,921 rows/sec
    30 days of daily rows       ~195,000 rows -> 6 SECONDS to store, ~56 MB
    per-row upsert throughput   498 rows/sec  -> 6.5 MINUTES for the same data

That last pair is why this table is written by delete-then-bulk-insert per day rather than the
SELECT-then-UPDATE-or-INSERT upsert used everywhere else in this app. A day's rows are wholly
replaced by a refetch, never merged, so there is nothing an upsert would preserve — and 62x is not
a micro-optimisation, it is the difference between a usable refresh and an unusable one.

**Bounded to a 30-day rolling window, purged nightly.** Production sits at 91% disk with 670 MB
free and `update-ec2.sh` copies the whole database before every deploy, so an unbounded daily table
would eventually fill the disk and break both SQLite writes and the deploy itself. 30 days is also
the longest range Amazon will answer in a single report, so it is the natural boundary.

No data migration: the table starts empty and the next refresh fills it. `ads_performance` is left
exactly as it is — the window-grain rows are still what a rule preview reads, and nothing that works
today changes behaviour.

Revision ID: d1a5c83b76e2
Revises: c9f4a2e17b83
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d1a5c83b76e2"
down_revision: Union[str, None] = "c9f4a2e17b83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ads_performance_daily",
        sa.Column("id", sa.Integer(), nullable=False),
        # A plain date string in Amazon's own reporting timezone. NEVER a DateTime: a timezone
        # conversion on a bare date is how the Orders tab once rendered a date as 05:30 the
        # following morning, and how this tab's own picker offered the 27th when the 28th was
        # available.
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("entity_id", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=12), nullable=False,
                  server_default="target"),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("ad_group_id", sa.String(length=32), nullable=True),
        sa.Column("text", sa.String(length=500), nullable=True),
        sa.Column("match_type", sa.String(length=40), nullable=True),
        # The bid AS AT that day. A sub-range sum takes the latest day's bid rather than adding
        # them — a sum of bids across days is a number that means nothing.
        sa.Column("reported_bid", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("spend", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("orders", sa.Integer(), nullable=True),
        sa.Column("sales", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_ads_daily_day_entity", "ads_performance_daily", ["day", "entity_id"], unique=True
    )
    # The index the sub-range sum reads: bounded by day, then grouped by entity.
    op.create_index("idx_ads_daily_day", "ads_performance_daily", ["day"])
    op.create_index(
        "idx_ads_daily_campaign", "ads_performance_daily", ["day", "campaign_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_ads_daily_campaign", table_name="ads_performance_daily")
    op.drop_index("idx_ads_daily_day", table_name="ads_performance_daily")
    op.drop_index("idx_ads_daily_day_entity", table_name="ads_performance_daily")
    op.drop_table("ads_performance_daily")
