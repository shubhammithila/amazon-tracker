"""ads: ad_product on every ads table, so Sponsored Brands can live beside Sponsored Products

The Ads tab only ever fetched `/sp/` endpoints, so Sponsored Brands was invisible — not filtered,
unimplemented. Measured on the live account: **6 SB campaigns (5 enabled, budgets Rs 4,000-20,000),
66 ad groups, 4,939 keywords all carrying editable bids.**

`ad_product` ("sp" | "sb") is added to all four ads tables rather than inferred, because the row
itself cannot say which product it belongs to: **`EXACT` is a legal match type for both a Sponsored
Products keyword and a Sponsored Brands keyword**, and they are written to different endpoints with
different payloads. Routing on match type alone would send SB ids to `/sp/keywords`.

A first-class column rather than a boolean, because Sponsored Display is a plausible third and
adding it should be a fetch plus a writer rather than a redesign.

`server_default="sp"` and `nullable=False`, so every existing row keeps its exact meaning with no
backfill: everything stored before this migration came from `/sp/`.

Also recorded on `ads_mutation`, the audit trail — an undo must go back to the SAME endpoint with the
same payload shape, and "which entity" does not imply "which API".

`batch_alter_table` because SQLite cannot add a NOT NULL column with a default in place; it is a
documented no-op on PostgreSQL.

Revision ID: e2b7d94c15af
Revises: d1a5c83b76e2
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e2b7d94c15af"
down_revision: Union[str, None] = "d1a5c83b76e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Every ads table that needs to know which Amazon ad product a row came from.
_TABLES = (
    "ads_entity",
    "ads_performance",
    "ads_performance_daily",
    "ads_mutation",
)


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column(
                    "ad_product",
                    sa.String(length=4),
                    nullable=False,
                    server_default="sp",
                )
            )

    # `(entity_type, entity_id)` was unique, and it still is: verified on the live account that the
    # SP and SB keyword id spaces do not overlap (0 collisions across 500 SP and 4,888 SB ids), so
    # widening the key is unnecessary. An index on ad_product instead, because every read filters
    # or groups by it.
    op.create_index("idx_ads_entity_product", "ads_entity", ["ad_product", "entity_type"])
    op.create_index("idx_ads_perf_product", "ads_performance", ["ad_product"])


def downgrade() -> None:
    op.drop_index("idx_ads_perf_product", table_name="ads_performance")
    op.drop_index("idx_ads_entity_product", table_name="ads_entity")
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("ad_product")
