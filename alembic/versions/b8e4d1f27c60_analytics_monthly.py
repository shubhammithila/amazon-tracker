"""analytics_monthly, analytics_traffic_monthly, analytics_refresh: Portfolio -> Analytics

Revision ID: b8e4d1f27c60
Revises: a7d3c9e15b20

Two years of monthly history (Nov 2024 onwards, all Data Kiosk keeps) per child ASIN, for the
Analytics sub-tab. Kept apart from economics_daily so the Profit tab's 90-day store is untouched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e4d1f27c60"
down_revision: Union[str, None] = "a7d3c9e15b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analytics_monthly",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("child_asin", sa.String(10), nullable=False),
        sa.Column("parent_asin", sa.String(10)),
        sa.Column("ordered_sales", sa.Numeric(12, 2)),
        sa.Column("refunded_sales", sa.Numeric(12, 2)),
        sa.Column("ad_spend", sa.Numeric(12, 2)),
        sa.Column("net_proceeds", sa.Numeric(12, 2)),
        sa.Column("units_ordered", sa.Integer),
        sa.Column("units_refunded", sa.Integer),
        sa.Column("net_units", sa.Integer),
        sa.Column("fees_json", sa.Text),
        sa.Column("ads_json", sa.Text),
        sa.Column("fetched_at", sa.DateTime),
    )
    op.create_index("idx_analytics_monthly_month_asin", "analytics_monthly", ["month", "child_asin"],
                    unique=True)
    op.create_table(
        "analytics_traffic_monthly",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("child_asin", sa.String(10), nullable=False),
        sa.Column("parent_asin", sa.String(10)),
        sa.Column("sessions", sa.Integer),
        sa.Column("page_views", sa.Integer),
        sa.Column("units_ordered", sa.Integer),
        sa.Column("buy_box_pct", sa.Numeric(6, 2)),
        sa.Column("sales", sa.Numeric(12, 2)),
        sa.Column("fetched_at", sa.DateTime),
    )
    op.create_index("idx_analytics_traffic_month_asin", "analytics_traffic_monthly",
                    ["month", "child_asin"], unique=True)
    op.create_table(
        "analytics_refresh",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("status", sa.String(12), nullable=False, server_default="running"),
        sa.Column("source", sa.String(12), nullable=False, server_default="nightly"),
        sa.Column("months", sa.Text),
        sa.Column("error", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("analytics_refresh")
    op.drop_index("idx_analytics_traffic_month_asin", table_name="analytics_traffic_monthly")
    op.drop_table("analytics_traffic_monthly")
    op.drop_index("idx_analytics_monthly_month_asin", table_name="analytics_monthly")
    op.drop_table("analytics_monthly")
