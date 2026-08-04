"""shipment sort priority, draft plans, row exclusion

Revision ID: e886574dd5f5
Revises: 469bf49dd801
Create Date: 2026-08-05 01:18:02.679847

Hand-trimmed after autogenerate, which got the shapes right and the details
wrong in one way that matters:

* ``brand_rank`` was emitted as ``nullable=False`` with **no server default**.
  That fails outright on any table that already holds rows — i.e. on the
  production database, which is the only place it matters. A ``server_default``
  plus an explicit backfill fixes it, and the default is then dropped so the
  application stays the only thing deciding the value.

``excluded_at`` needs no backfill by design: the exclusion filter is
``WHERE excluded_at IS NULL``, so every pre-existing row is already "included".

No data is read from ``product_families.json`` here. Category rows are seeded at
runtime by ``repository.ensure_categories``, for the same reason
``/shipment/import-legacy`` is a route rather than a migration: file I/O does not
belong in schema history, and a migration that depends on a JSON file that may
have moved is a migration that cannot be replayed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e886574dd5f5'
down_revision: Union[str, None] = '469bf49dd801'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'product_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_key', sa.String(length=120), nullable=False),
        sa.Column('product_label', sa.Text(), nullable=True),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='6'),
        sa.Column('source', sa.String(length=10), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('product_categories', schema=None) as batch_op:
        batch_op.create_index('idx_product_categories_key', ['product_key'], unique=True)

    with op.batch_alter_table('shipment_plan_items', schema=None) as batch_op:
        # server_default so this survives a table that already has rows.
        batch_op.add_column(
            sa.Column('brand_rank', sa.Integer(), nullable=False, server_default='2')
        )
        batch_op.add_column(sa.Column('excluded_at', sa.DateTime(), nullable=True))

    # Backfill from the brand code already on the row. 2 (unknown) is the default
    # and stays for anything that is neither, which sorts last — a new brand at
    # the TOP of the packing sheet would read as a broken sheet.
    op.execute(
        """
        UPDATE shipment_plan_items
           SET brand_rank = CASE UPPER(TRIM(COALESCE(brand, '')))
                                WHEN 'MF' THEN 0
                                WHEN 'HF' THEN 1
                                ELSE 2
                            END
        """
    )

    # Drop the server default now the column is populated, so the application is
    # the only thing that decides a brand rank from here on.
    with op.batch_alter_table('shipment_plan_items', schema=None) as batch_op:
        batch_op.alter_column('brand_rank', server_default=None)


def downgrade() -> None:
    """Reversible, with one honest caveat.

    Dropping ``excluded_at`` silently un-excludes every row the owner removed,
    and dropping ``product_categories`` reverts every hand-set priority to a
    keyword guess. Nothing errors; the plan simply comes back with rows in it that
    were deliberately taken out. Worth knowing before running this against a
    database someone has been using.
    """
    with op.batch_alter_table('shipment_plan_items', schema=None) as batch_op:
        batch_op.drop_column('excluded_at')
        batch_op.drop_column('brand_rank')

    with op.batch_alter_table('product_categories', schema=None) as batch_op:
        batch_op.drop_index('idx_product_categories_key')

    op.drop_table('product_categories')
