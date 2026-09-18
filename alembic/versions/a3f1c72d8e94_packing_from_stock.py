"""packing entries record how much came off the shelf rather than being made today

Asked for so the packed sheet ops prints for accounts can distinguish the two. The packer enters
"made today" and "from stock" and they ADD UP: 40 made + 50 from stock = 90 units into today's
cartons, and 90 is what goes to FBA.

``units`` therefore keeps its exact existing meaning — the TOTAL boxed — because every downstream
figure already reads it: ``remaining_for``, ``over_packed``, ``_recompute_day_units``, the GST
invoice payload and the Amazon upload quantity. Only the provenance is new.

``server_default="0"`` matters here beyond tidiness: every existing row predates the column and must
read as "all of it was made today", which is what the data actually says. A nullable column would
make "no value" ambiguous between that and "nobody has recorded it".

Revision ID: a3f1c72d8e94
Revises: 793508bceef8
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "a3f1c72d8e94"
down_revision = "793508bceef8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `batch_alter_table` because SQLite cannot ALTER a column in place. It is a harmless no-op on
    # Postgres — `PostgresqlImpl.requires_recreate_in_batch` returns False unconditionally — which is
    # what keeps the deferred Postgres move unaffected.
    with op.batch_alter_table("shipment_packing_entries") as batch:
        batch.add_column(
            sa.Column(
                "from_stock",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("shipment_packing_entries") as batch:
        batch.drop_column("from_stock")
