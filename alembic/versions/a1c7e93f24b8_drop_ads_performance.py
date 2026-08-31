"""ads: drop ads_performance — the per-day rows are the only grain

**Two tables answered the same question and disagreed by 28% of spend.**

`ads_performance` held one row set per date window; `ads_performance_daily` holds one row per entity
per day. The refresh wrote Sponsored Brands to the window table and NOT to the daily table, and the
read side preferred the window table when that exact range had been fetched, falling back to summing
daily rows otherwise. So which figure you got depended on whether somebody had happened to fetch
those endpoints: 22-28 Aug reported Rs 4,44,550 and 22-29 Aug — a strict superset — reported
Rs 3,34,300. Rs 1,26,328 of real spend read as zero.

Worse on `/ads/preview`, the only route in this app that spends money: the same rule found **1,005
changes including 296 Sponsored Brands rows** on a fetched window and **743 with none** on a derived
one, so 296 live SB bids were invisible to a rule meant to act on them.

**Nothing of value is lost.** Every row here is reproducible from the daily rows or a refetch, and
the table was the largest in the database (105,755 rows / 17.1 MB) purely to cache figures the daily
rows already hold.

Two changes to `ads_performance_daily` in the same revision, because both are consequences of it now
holding more than one ad product:

* the unique key gains `ad_product`. It was `(day, entity_id)`, which is correct while only one
  product is stored and makes two products' rows for one day mutually exclusive as soon as an id
  collides. SP and SB are separate APIs with separate id spaces; 0 collisions across 29,360 ids
  today is luck rather than a guarantee.
* an index on `(day, ad_product)`, which is what every write deletes by and what
  `daily_range_complete` asks for.

The downgrade recreates `ads_performance` EMPTY. It cannot restore the rows, and pretending
otherwise would be worse than saying so: the data is refetchable, the schema is what a downgrade
owes.

Revision ID: a1c7e93f24b8
Revises: e2b7d94c15af
Create Date: 2026-08-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1c7e93f24b8"
down_revision: Union[str, None] = "e2b7d94c15af"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `idx_ads_perf_product` lives on this table and goes with it; dropping the table drops its
    # indexes, so it is not dropped separately.
    op.drop_table("ads_performance")

    # The unique key must admit one row per (day, entity, PRODUCT). `batch_alter_table` because
    # SQLite cannot redefine an index in place; a documented no-op on PostgreSQL.
    op.drop_index("idx_ads_daily_day_entity", table_name="ads_performance_daily")
    op.create_index(
        "idx_ads_daily_day_entity",
        "ads_performance_daily",
        ["day", "entity_id", "ad_product"],
        unique=True,
    )
    op.create_index(
        "idx_ads_daily_day_product", "ads_performance_daily", ["day", "ad_product"]
    )


def downgrade() -> None:
    op.drop_index("idx_ads_daily_day_product", table_name="ads_performance_daily")
    op.drop_index("idx_ads_daily_day_entity", table_name="ads_performance_daily")
    op.create_index(
        "idx_ads_daily_day_entity", "ads_performance_daily", ["day", "entity_id"], unique=True
    )

    op.create_table(
        "ads_performance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(length=10), nullable=False),
        sa.Column("window_end", sa.String(length=10), nullable=False),
        sa.Column("entity_id", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=12), nullable=False, server_default="target"),
        sa.Column("ad_product", sa.String(length=4), nullable=False, server_default="sp"),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("ad_group_id", sa.String(length=32), nullable=True),
        sa.Column("text", sa.String(length=500), nullable=True),
        sa.Column("match_type", sa.String(length=40), nullable=True),
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
        "idx_ads_perf_window_entity",
        "ads_performance",
        ["window_start", "window_end", "entity_id"],
        unique=True,
    )
    op.create_index(
        "idx_ads_perf_window_campaign",
        "ads_performance",
        ["window_start", "window_end", "campaign_id"],
    )
    # Recreated so a further downgrade to before e2b7d94c15af can drop it, as that revision expects.
    op.create_index("idx_ads_perf_product", "ads_performance", ["ad_product"])
