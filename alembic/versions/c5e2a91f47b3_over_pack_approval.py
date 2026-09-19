"""the owner can approve packing more than the plan asked for

A red banner reading "More packed than planned on 2 row(s). Moringa Sattu 1 kg planned 60, packed
62 (+2)" sat on both the owner's Shipment dashboard and the warehouse packing screen with no way to
resolve it except raising the plan or unpacking the boxes. Reported as "extra packing kar diya to
theek hai na. puch hi ke kiya" — the packer asked first, so a decision already taken kept rendering
as an unresolved error, which is how a permanent red banner trains its reader to skip the one that
matters.

**The banner is not wrong**, which is the whole constraint: ``POST /shipment/invoice-payload`` bills
``logic.units_by_asin`` — the PACKED units — so 62 boxed against a plan of 60 really does put 62 on
a GST invoice. So this is an APPROVAL, not a hide button, and it reaches no quantity anywhere.

``over_pack_approved_units`` stores the packed TOTAL signed off, never the excess. Comparing against
``max(planned, approved)`` means raising the plan 60 -> 100 SUPERSEDES the approval; storing the
excess and comparing against ``planned + approved`` would silently move the threshold to 102 and
authorise an overage nobody looked at.

**NULLABLE, deliberately — the opposite of a3f1c72d8e94's ``server_default="0"``.** There, every
pre-existing row had a real fact to read as ("all of it was made today"). Here the fact is "nobody
has decided", and a default of 0 would make that indistinguishable from "approved zero units".

Revision ID: c5e2a91f47b3
Revises: a3f1c72d8e94
Create Date: 2026-09-19
"""
from alembic import op
import sqlalchemy as sa

revision = "c5e2a91f47b3"
down_revision = "a3f1c72d8e94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `batch_alter_table` because SQLite cannot ALTER a column in place. It is a harmless no-op on
    # Postgres — `PostgresqlImpl.requires_recreate_in_batch` returns False unconditionally — which is
    # what keeps the deferred Postgres move unaffected.
    with op.batch_alter_table("shipment_plan_items") as batch:
        batch.add_column(sa.Column("over_pack_approved_units", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("over_pack_approved_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("shipment_plan_items") as batch:
        batch.drop_column("over_pack_approved_at")
        batch.drop_column("over_pack_approved_units")
