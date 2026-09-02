"""projections: drop the now-global growth_rate column

**Additive-only companion, in reverse: this DROPS one column, nothing else.** `growth_rate` on
`projection_row` is dead the moment growth becomes a single account-wide setting
(`projection_blend.global_growth_rate`, via `app.projections.logic.DEFAULT_BLEND`) rather than a
per-parent field — measured against `app/invoice/projection_defaults.json`, 79 of its 81 static
entries already used the same 0.3, so this was a company-wide assumption typed once per product by
accident of that file's structure, not a genuine per-product signal.

`batch_alter_table` because SQLite cannot DROP COLUMN before 3.35 and Alembic's batch mode rebuilds
the table instead; PostgreSQL ignores the batching and issues a native ALTER. Same reasoning as
0f85fa400957 (`shipment_packing_entries.cartons` dropped for the identical class of reason: a field
that no longer means anything once the concept moved elsewhere).

Revision ID: db7f8bc09d4d
Revises: e81434e50028
Create Date: 2026-09-02 17:56:34.804741

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db7f8bc09d4d"
down_revision: Union[str, None] = "e81434e50028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projection_row", schema=None) as batch_op:
        batch_op.drop_column("growth_rate")


def downgrade() -> None:
    """Restore the column at the old default. The per-parent VALUES are genuinely gone — this
    codebase's static file had already reduced them to one company-wide number in practice
    (79/81 entries at 0.3), so restoring at the shared default loses nothing that mattered."""
    with op.batch_alter_table("projection_row", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("growth_rate", sa.Numeric(6, 2), nullable=True, server_default="0.3")
        )
