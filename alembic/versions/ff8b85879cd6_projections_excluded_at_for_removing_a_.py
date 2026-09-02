"""projections: excluded_at for removing a row

**Additive only** — one nullable column, matching ShipmentPlanItem.excluded_at exactly. Every
existing row gets NULL (= included), so nothing already on screen disappears from this
migration alone.

Revision ID: ff8b85879cd6
Revises: db7f8bc09d4d
Create Date: 2026-09-02 19:09:57.346399

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ff8b85879cd6"
down_revision: Union[str, None] = "db7f8bc09d4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projection_row", sa.Column("excluded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projection_row", schema=None) as batch_op:
        batch_op.drop_column("excluded_at")
