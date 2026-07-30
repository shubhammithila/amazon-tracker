"""add products.use_by

Revision ID: 68e373db239a
Revises: da6e9b47821c
Create Date: 2026-07-30 15:39:40.635738

Conditional on purpose. Two different starting states have to converge here:

  * Databases that predate Alembic (the original tracker.db) were stamped
    directly at da6e9b47821c while physically missing products.use_by, so they
    genuinely need the ADD COLUMN.
  * A brand new database runs da6e9b47821c properly, and that revision already
    creates use_by as part of the products table — so an unconditional
    ADD COLUMN fails with "duplicate column name: use_by" and no fresh deploy
    can migrate at all.

Inspecting the live table keeps both paths working.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '68e373db239a'
down_revision: Union[str, None] = 'da6e9b47821c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _products_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns("products")}


def upgrade() -> None:
    if "use_by" not in _products_columns():
        with op.batch_alter_table("products", schema=None) as batch_op:
            batch_op.add_column(sa.Column("use_by", sa.String(length=50), nullable=True))


def downgrade() -> None:
    if "use_by" in _products_columns():
        with op.batch_alter_table("products", schema=None) as batch_op:
            batch_op.drop_column("use_by")
