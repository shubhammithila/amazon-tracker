"""ads_search_term_daily, ads_mutation.rule_window, ads_refresh.search_*: negatives from search terms

Revision ID: e9b4c2d7a813
Revises: d7a3e5c19b42

Per-day spend and sales per search term per ad group (SP and SB), so a rule can add an overspending
search term as a negative exact keyword (or, for an ASIN term, a negative product target) in that ad
group. `rule_window` records the window a rule ran on, so the saved-rules panel can say "ran today"
per time period.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9b4c2d7a813"
down_revision: Union[str, None] = "d7a3e5c19b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ads_search_term_daily",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("ad_product", sa.String(4), nullable=False, server_default="sp"),
        sa.Column("campaign_id", sa.String(32)),
        sa.Column("campaign_name", sa.String(300)),
        sa.Column("ad_group_id", sa.String(32)),
        sa.Column("ad_group_name", sa.String(300)),
        sa.Column("keyword_id", sa.String(32)),
        sa.Column("keyword_text", sa.String(300)),
        sa.Column("match_type", sa.String(40)),
        sa.Column("targeting", sa.String(300)),
        sa.Column("search_term", sa.String(300), nullable=False),
        sa.Column("impressions", sa.Integer),
        sa.Column("clicks", sa.Integer),
        sa.Column("cost", sa.Numeric(12, 2)),
        sa.Column("orders", sa.Integer),
        sa.Column("sales", sa.Numeric(12, 2)),
        sa.Column("fetched_at", sa.DateTime),
    )
    op.create_index("idx_ads_search_term_day", "ads_search_term_daily", ["day", "ad_product"])
    op.create_index("idx_ads_search_term_group", "ads_search_term_daily", ["ad_group_id"])
    with op.batch_alter_table("ads_mutation") as batch:
        batch.add_column(sa.Column("rule_window", sa.String(23)))
    with op.batch_alter_table("ads_refresh") as batch:
        batch.add_column(sa.Column("search_rows", sa.Integer, server_default="0"))
        batch.add_column(sa.Column("search_error", sa.Text))


def downgrade() -> None:
    with op.batch_alter_table("ads_refresh") as batch:
        batch.drop_column("search_error")
        batch.drop_column("search_rows")
    with op.batch_alter_table("ads_mutation") as batch:
        batch.drop_column("rule_window")
    op.drop_index("idx_ads_search_term_group", table_name="ads_search_term_daily")
    op.drop_index("idx_ads_search_term_day", table_name="ads_search_term_daily")
    op.drop_table("ads_search_term_daily")
