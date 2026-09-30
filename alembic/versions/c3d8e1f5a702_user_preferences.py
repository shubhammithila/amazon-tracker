"""users: personal display preferences (Portfolio column layout)

Asked for as "make every column … hiddenable" and "make the columns slidable", saved "with your
login" so the layout follows the owner across devices. One nullable JSON text column, namespaced by
screen, so another tab can store its own choice later with no further migration.

NULL, not `server_default='{}'`: "never chosen" and "chose the default" are different facts, and
only NULL lets a later default change reach users who never customised — the
`over_pack_approved_units` precedent rather than the `from_stock` one.

Revision ID: c3d8e1f5a702
Revises: b91d4a7c3e26
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d8e1f5a702"
down_revision = "b91d4a7c3e26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table: SQLite cannot ALTER in place; a no-op on Postgres.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("preferences_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("preferences_json")
