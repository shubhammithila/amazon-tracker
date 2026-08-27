"""portfolio economics

Three tables behind the rebuilt Portfolio tab, which replaces a CSV upload with Amazon's own
Seller Central Economics figures pulled live through Data Kiosk.

economics_snapshot  — the fetched rows, cached because a Data Kiosk query takes 1-2 minutes and
                      must never be awaited inside a request.
economics_refresh   — one row per run, so the screen can say how old the numbers are.
product_decision    — the owner's kill/keep/watch plus a note and the figures at the time.
                      Supersedes discontinued_products.json, which stored a bare name in a set
                      with no date, no reason and no numbers.

Fees and ad charges are JSON text rather than typed columns: Amazon returned 8 distinct fee
types on this account and adds more over time, so a column each would mean a migration every
time Amazon invents a fee.

No data migration. The tables start empty and the first refresh fills them; the old JSON blobs
were untracked and are simply left on disk.

Revision ID: a7c4e91b58d2
Revises: f6b2d4907ae3
Create Date: 2026-08-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a7c4e91b58d2"
down_revision: Union[str, None] = "f6b2d4907ae3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "economics_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(length=10), nullable=False),
        sa.Column("window_end", sa.String(length=10), nullable=False),
        sa.Column("child_asin", sa.String(length=10), nullable=False),
        sa.Column("parent_asin", sa.String(length=10), nullable=True),
        sa.Column("ordered_sales", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("refunded_sales", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("ad_spend", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("net_proceeds", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("units_ordered", sa.Integer(), nullable=True),
        sa.Column("units_refunded", sa.Integer(), nullable=True),
        sa.Column("net_units", sa.Integer(), nullable=True),
        sa.Column("fees_json", sa.Text(), nullable=True),
        sa.Column("ads_json", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE: re-running a refresh for the same window must UPDATE its rows, never double the
    # portfolio — the same guarantee (pack_date, asin) gives the warehouse's packed counts.
    op.create_index(
        "idx_economics_snapshot_window_asin",
        "economics_snapshot",
        ["window_start", "window_end", "child_asin"],
        unique=True,
    )

    op.create_table(
        "economics_refresh",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(length=10), nullable=True),
        sa.Column("window_end", sa.String(length=10), nullable=True),
        sa.Column("rows_stored", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "product_decision",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("parent_asin", sa.String(length=10), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # One standing decision per product: re-deciding replaces it rather than appending, so the
    # dashboard never has to guess which of two rows is current.
    op.create_index(
        "idx_product_decision_parent", "product_decision", ["parent_asin"], unique=True
    )


def downgrade() -> None:
    op.drop_index("idx_product_decision_parent", table_name="product_decision")
    op.drop_table("product_decision")
    op.drop_table("economics_refresh")
    op.drop_index("idx_economics_snapshot_window_asin", table_name="economics_snapshot")
    op.drop_table("economics_snapshot")
