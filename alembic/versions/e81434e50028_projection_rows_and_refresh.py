"""projections: parent rows (name-keyed) and a weekly-refresh record

**Additive only** — two new tables, nothing existing touched, so this is safe on a populated
database and the downgrade is exact.

`projection_row` replaces `app/invoice/projection_defaults.json` and `projection_data.json` (a
flat file at repo root) as the source of truth for the Projections tab: keyed on the parent
product NAME (matching `product_raw_stock`'s own choice, for the same reason — this is bulk
purchasing, and the MRP sheet's name is the one identifier a brand-new product carries from day
one). `sales_source` is what lets a hand-typed sales override survive the weekly recompute that
`projection_refresh` records the history of.

Revision ID: e81434e50028
Revises: c5e91a3d47b6
Create Date: 2026-09-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e81434e50028"
down_revision: Union[str, None] = "c5e91a3d47b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projection_row",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("parent_product", sa.String(length=120), nullable=False),
        sa.Column("brand", sa.String(length=60), nullable=True),
        sa.Column("purchase_rate", sa.Numeric(10, 2), nullable=True, server_default="0"),
        sa.Column("supplier_to_wh", sa.Integer(), nullable=True, server_default="5"),
        sa.Column("packing", sa.Integer(), nullable=True, server_default="2"),
        sa.Column("wh_to_ixd", sa.Integer(), nullable=True, server_default="10"),
        sa.Column("ixd_to_fba", sa.Integer(), nullable=True, server_default="5"),
        sa.Column("wh_buffer_days", sa.Numeric(6, 1), nullable=True, server_default="10"),
        sa.Column("seasonal_impact", sa.Numeric(6, 2), nullable=True, server_default="1.0"),
        sa.Column("growth_rate", sa.Numeric(6, 2), nullable=True, server_default="0.3"),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sales_source", sa.String(length=10), nullable=False, server_default="sheet"),
        sa.Column("last_month_sale", sa.Numeric(10, 2), nullable=True, server_default="0"),
        sa.Column("seven_day_rate", sa.Numeric(10, 2), nullable=True),
        sa.Column("thirty_day_rate", sa.Numeric(10, 2), nullable=True),
        sa.Column("daily_rate", sa.Numeric(10, 2), nullable=True, server_default="0"),
        sa.Column("diverged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("current_fba_stock", sa.Numeric(10, 1), nullable=True, server_default="0"),
        sa.Column("current_wh_stock", sa.Numeric(10, 1), nullable=True, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(length=60), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_projection_row_parent", "projection_row", ["parent_product"], unique=True
    )

    op.create_table(
        "projection_refresh",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(length=10), nullable=True),
        sa.Column("window_end", sa.String(length=10), nullable=True),
        sa.Column("rows_stored", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("projection_refresh")
    op.drop_index("idx_projection_row_parent", table_name="projection_row")
    op.drop_table("projection_row")
