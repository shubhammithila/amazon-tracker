"""ads: record each refresh run, so a partial night can explain itself after a restart

**This table exists because its absence cost a morning of a dashboard reading Rs 0.**

On 1 Sep the nightly ads refresh stored 482,578 Sponsored Products rows across 59 days and Amazon then
rate-limited the Sponsored Brands report, storing **0**. `daily_range_complete` correctly refused every
window outside the 7 days SB still held — mixing a complete product with an incomplete one would
understate spend, and a bid rule would act on the short figure. The refresh recorded all of this
faithfully, in a module-level `STATE` dict. A deploy then restarted the app, the dict reset, and the
Ads tab reported "nothing fetched" with no way to learn that half a million current rows were sitting
in the table and one throttled report was the whole problem.

The reason a screen is empty must outlive the process that discovered it. `economics_refresh` has done
this for the Portfolio tab since that feature shipped; this is its sibling.

**`sp_rows` and `sb_rows` are separate columns rather than one total.** `0 SB` beside `482,578 SP` IS
the finding; a single `rows_stored` of 482,578 reads as a completely successful night.

**Additive only** — one new table, no existing table touched, no data read or rewritten — so this is
safe to run on a populated database and the downgrade is exact.

Revision ID: c5e91a3d47b6
Revises: b4d8f27ac913
Create Date: 2026-09-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e91a3d47b6"
down_revision: Union[str, None] = "b4d8f27ac913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ads_refresh",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(length=10), nullable=True),
        sa.Column("window_end", sa.String(length=10), nullable=True),
        # `done` / `partial` / `failed`. `partial` is the state this table was added for.
        sa.Column("status", sa.String(length=10), nullable=False, server_default="done"),
        sa.Column("sp_rows", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("sb_rows", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("campaigns", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("ad_groups", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        # Kept apart from `error`: "the refresh failed" is wrong when 72% of the spend updated fine.
        sa.Column("sb_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # `last_refresh` orders by `started_at DESC, id DESC` on every page load of the Ads tab, and this
    # table grows by one row a night plus one per manual refresh — small, but the index is free and a
    # sort over a year of runs on every load is not.
    op.create_index("idx_ads_refresh_started", "ads_refresh", ["started_at"])


def downgrade() -> None:
    op.drop_index("idx_ads_refresh_started", table_name="ads_refresh")
    op.drop_table("ads_refresh")
