"""ads: index ads_mutation.created_at for the bid-change log and the once-per-day guard

Two new readers scan this column, and the table's expected size changed at the same time.

* **The bid log** filters by date range — "what did we change last week".
* **The once-per-day guard** asks, on every preview and every apply, what we already changed today.
  A preview matching 1,005 rows looks up all of them.

`ads_mutation` also now has a retention of **365 days** rather than none, so it is expected to hold a
year of runs: at three 1,000-row runs a day that is ~1,000,000 rows, where an unindexed range scan is
what turns a page that loads into a page that times out. The existing indexes cover `run_id` and
`entity_id`; nothing covered time.

**Index only — no data is read, written or deleted**, so this is safe to run on a populated table and
the downgrade is exact.

Revision ID: b4d8f27ac913
Revises: a1c7e93f24b8
Create Date: 2026-08-31

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d8f27ac913"
down_revision: Union[str, None] = "a1c7e93f24b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_ads_mutation_created", "ads_mutation", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_ads_mutation_created", table_name="ads_mutation")
