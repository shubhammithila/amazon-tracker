"""shipment plan tables

Moves the shipment plan out of the shipment_plan.json blob at repo root and into
four tables, so the owner (plan quantities) and the operations employee (daily
packing) can write concurrently without overwriting each other.

The two unique indexes are load-bearing, not just for speed:
  idx_packing_days_plan_date   one packing day per plan per calendar date
  idx_packing_entries_day_asin one entry per SKU per day, so a repeated save
                               upserts instead of double-counting the units

Revision ID: 469bf49dd801
Revises: 68e373db239a
Create Date: 2026-07-30 18:38:43.099527

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '469bf49dd801'
down_revision: Union[str, None] = '68e373db239a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('shipment_plans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.Column('multiplier', sa.Numeric(precision=4, scale=1), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.Column('min_cartons', sa.Integer(), nullable=True),
    sa.Column('min_units', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('shipment_packing_days',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('plan_id', sa.Integer(), nullable=False),
    sa.Column('pack_date', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=True),
    sa.Column('hold_reason', sa.Text(), nullable=True),
    sa.Column('total_units', sa.Integer(), nullable=True),
    sa.Column('total_cartons', sa.Integer(), nullable=True),
    sa.Column('submitted_by', sa.String(length=20), nullable=True),
    sa.Column('submitted_at', sa.DateTime(), nullable=True),
    sa.Column('verified_at', sa.DateTime(), nullable=True),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ),
    sa.ForeignKeyConstraint(['plan_id'], ['shipment_plans.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('shipment_packing_days', schema=None) as batch_op:
        batch_op.create_index('idx_packing_days_plan_date', ['plan_id', 'pack_date'], unique=True)

    op.create_table('shipment_plan_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('plan_id', sa.Integer(), nullable=False),
    sa.Column('asin', sa.String(length=10), nullable=False),
    sa.Column('fba_sku', sa.String(length=100), nullable=True),
    sa.Column('brand', sa.String(length=4), nullable=True),
    sa.Column('item', sa.Text(), nullable=True),
    sa.Column('sort_product', sa.String(length=120), nullable=True),
    sa.Column('weight', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('sales_7d', sa.Integer(), nullable=True),
    sa.Column('projection', sa.Integer(), nullable=True),
    sa.Column('fba_stock', sa.Integer(), nullable=True),
    sa.Column('deficit', sa.Integer(), nullable=True),
    sa.Column('shipment_plan', sa.Integer(), nullable=True),
    sa.Column('available', sa.Integer(), nullable=True),
    sa.Column('s', sa.Boolean(), nullable=True),
    sa.Column('m', sa.Boolean(), nullable=True),
    sa.Column('b', sa.Boolean(), nullable=True),
    sa.ForeignKeyConstraint(['plan_id'], ['shipment_plans.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('shipment_plan_items', schema=None) as batch_op:
        batch_op.create_index('idx_shipment_plan_items_plan_asin', ['plan_id', 'asin'], unique=False)

    op.create_table('shipment_packing_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('day_id', sa.Integer(), nullable=False),
    sa.Column('asin', sa.String(length=10), nullable=False),
    sa.Column('units', sa.Integer(), nullable=True),
    sa.Column('cartons', sa.Integer(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['day_id'], ['shipment_packing_days.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('shipment_packing_entries', schema=None) as batch_op:
        batch_op.create_index('idx_packing_entries_day_asin', ['day_id', 'asin'], unique=True)



def downgrade() -> None:
    with op.batch_alter_table('shipment_packing_entries', schema=None) as batch_op:
        batch_op.drop_index('idx_packing_entries_day_asin')

    op.drop_table('shipment_packing_entries')
    with op.batch_alter_table('shipment_plan_items', schema=None) as batch_op:
        batch_op.drop_index('idx_shipment_plan_items_plan_asin')

    op.drop_table('shipment_plan_items')
    with op.batch_alter_table('shipment_packing_days', schema=None) as batch_op:
        batch_op.drop_index('idx_packing_days_plan_date')

    op.drop_table('shipment_packing_days')
    op.drop_table('shipment_plans')
