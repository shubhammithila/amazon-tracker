"""ads_mutation gains action, old_state and new_state.

A pause is a different KIND of change from a bid edit, and `build_undo` must be able to tell them
apart — a null `new_bid` cannot serve as the signal, because that is also what a crashed run leaves
behind.

`action` carries a `server_default` of "bid" so the ~2,900 existing rows keep their meaning and no
back-fill is needed. The state columns are nullable for the mirror-image reason: a bid row has no
state, and the two pairs are mutually exclusive by construction in `open_run`.

Revision ID: bf1c526cd768
Revises: d3479a8ed8ad
Create Date: 2026-09-04 11:06:49.788280

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bf1c526cd768'
down_revision: Union[str, None] = 'd3479a8ed8ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("ads_mutation") as batch:
        batch.add_column(sa.Column("action", sa.String(length=8), nullable=False,
                                   server_default="bid"))
        batch.add_column(sa.Column("old_state", sa.String(length=12), nullable=True))
        batch.add_column(sa.Column("new_state", sa.String(length=12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ads_mutation") as batch:
        batch.drop_column("new_state")
        batch.drop_column("old_state")
        batch.drop_column("action")
