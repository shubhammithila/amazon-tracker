"""product_page, growth_verdict, growth_suggestion: Ads keyword growth

Revision ID: a1c5e8f20b74
Revises: e9b4c2d7a813

The product page text from the Catalog API (cached 7 days), Claude's keep/reject verdict per product and
candidate (reused 30 days), and the last Find's suggestions per ad group, so Add is rebuilt from the
server's own list rather than from what a browser sends.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c5e8f20b74"
down_revision: Union[str, None] = "e9b4c2d7a813"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_page",
        sa.Column("asin", sa.String(10), primary_key=True),
        sa.Column("title", sa.Text),
        sa.Column("bullets_json", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("fetched_at", sa.DateTime),
    )
    op.create_table(
        "growth_verdict",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_key", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("term", sa.String(300), nullable=False),
        sa.Column("keep", sa.Boolean, nullable=False),
        sa.Column("reason", sa.String(300)),
        sa.Column("judged_at", sa.DateTime),
    )
    op.create_index("uq_growth_verdict", "growth_verdict", ["product_key", "kind", "term"], unique=True)
    op.create_table(
        "growth_suggestion",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ad_group_id", sa.String(32), nullable=False),
        sa.Column("key", sa.String(400), nullable=False),
        sa.Column("ad_product", sa.String(4), nullable=False),
        sa.Column("campaign_id", sa.String(32)),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("term", sa.String(300), nullable=False),
        sa.Column("sources_json", sa.Text),
        sa.Column("figures_json", sa.Text),
        sa.Column("keep", sa.Boolean, nullable=False),
        sa.Column("reason", sa.String(300)),
        sa.Column("bids_json", sa.Text),
        sa.Column("found_at", sa.DateTime),
    )
    op.create_index("uq_growth_suggestion", "growth_suggestion", ["ad_group_id", "key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_growth_suggestion", table_name="growth_suggestion")
    op.drop_table("growth_suggestion")
    op.drop_index("uq_growth_verdict", table_name="growth_verdict")
    op.drop_table("growth_verdict")
    op.drop_table("product_page")
