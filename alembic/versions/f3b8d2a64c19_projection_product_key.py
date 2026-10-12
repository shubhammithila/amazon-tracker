"""projection_row.product_key: one config row per Profit-tab product

Revision ID: f3b8d2a64c19
Revises: e5a9c3f17d42

The Projections tab now lists exactly the Profit tab's product rows (one per flavour), keyed by the
same id. Names are no longer unique there (the catalogue repeats some, such as Singhara Atta x4),
so the name index stops being unique and the key gets the unique index. Existing rows keep their
lead times and are matched by name until they are next saved.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8d2a64c19"
down_revision: Union[str, None] = "e5a9c3f17d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projection_row") as batch:
        batch.add_column(sa.Column("product_key", sa.String(20), nullable=True))
        batch.drop_index("idx_projection_row_parent")
        batch.create_index("idx_projection_row_parent", ["parent_product"])
        batch.create_index("idx_projection_row_key", ["product_key"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("projection_row") as batch:
        batch.drop_index("idx_projection_row_key")
        batch.drop_index("idx_projection_row_parent")
        batch.create_index("idx_projection_row_parent", ["parent_product"], unique=True)
        batch.drop_column("product_key")
