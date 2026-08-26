"""product_raw_stock

Raw material on hand per parent product, in kilograms, feeding the Orders tab's purchasing
view (to_buy = ordered_kg - raw_kg, clamped at 0).

Standing rather than per-day, unlike order_packed_entries: raw material on a shelf does not
vanish at midnight, and a dated row would be blank every morning — the purchasing tab would
demand 33 numbers be retyped before it meant anything.

Keyed on the parent product name rather than an ASIN because raw material is bulk: there is no
such thing as 500 g-flavoured raw sattu.

No data migration: the table starts empty and the screen fills it. Later the inventory tab will
write it instead of a person.

Revision ID: e5a1b83c26df
Revises: d4f9a2c68b31
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e5a1b83c26df"
down_revision: Union[str, None] = "d4f9a2c68b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_raw_stock",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product", sa.String(length=120), nullable=False),
        sa.Column("raw_kg", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE: a repeated save must UPDATE one row, never store a second standing quantity for
    # the same product — the same property (pack_date, asin) gives packed counts.
    op.create_index(
        "idx_product_raw_stock_product", "product_raw_stock", ["product"], unique=True
    )


def downgrade() -> None:
    op.drop_index("idx_product_raw_stock_product", table_name="product_raw_stock")
    op.drop_table("product_raw_stock")
