"""ads_rule gains target_state, so a PAUSE rule can be saved at all.

`amount` is `Numeric(12, 2)`. A pause rule's amount is the word "PAUSED", so `POST /ads/rules`
returned 500 with `could not convert string to float: 'PAUSED'` and the rule never saved — reported as
"I just saved a rule but it is not showing".

A separate nullable column rather than widening `amount` to text: one field holding either a number or
a word means every reader has to guess which, and widening would silently turn every existing saved
percentage into a string. Nullable, so the existing rows need no back-fill — they are all bid rules
and their `target_state` is correctly empty.

Revision ID: 793508bceef8
Revises: bf1c526cd768
Create Date: 2026-09-13 15:32:10.634473

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '793508bceef8'
down_revision: Union[str, None] = 'bf1c526cd768'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("ads_rule") as batch:
        batch.add_column(sa.Column("target_state", sa.String(length=12), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ads_rule") as batch:
        batch.drop_column("target_state")
