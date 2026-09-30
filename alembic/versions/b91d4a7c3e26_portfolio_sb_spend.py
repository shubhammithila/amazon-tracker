"""portfolio: Sponsored Brands spend attributed per ASIN per day

Asked as *"the ad expenses which you are taking includes all SP, SB, SD campaigns right?"* — and the
answer was no. The Portfolio tab's ad spend comes from the SP-API economics feed, and across all
9,074 rows that carry ad spend that feed returns exactly ONE ad type name: ``SponsoredProductFee``.
Measured on the 30-day window it was reported against, that left **Rs 5,30,615 of Sponsored Brands
spend — 28% of the real total — invisible**, so TACOS was understated and net margin overstated on
every row and in all four KPI tiles.

Amazon has no ASIN-level SB cost report (``sbAdvertisedProduct`` does not exist; ``sbPurchasedProduct``
carries no ``cost`` column), but ``POST /sb/v4/ads/list`` DOES declare which ASINs each ad group
advertises via ``creative.asins``. Joined against the SB spend we already store per ad group, that
covers **97.9%** of it. So the figure is derived from Amazon's own declaration rather than guessed,
and the remainder is spread across the portfolio by sales share.

── Why two columns on THIS table rather than a sibling table ──

The figure is per ``(day, child_asin)``, which is exactly the key ``economics_daily`` already has and
already has a unique index on. A sibling table would be a second thing for ``range_completeness``,
``days_held`` and ``purge_daily`` to agree about — and CLAUDE.md records at length what happened when
one figure lived in two caches: a superset window reported LESS than its subset, and Rs 1,26,328 of SB
spend went missing from the Ads tab. Keeping it here also means retention needs no change at all: the
SB figure ages out with the row that carries it.

**It must NOT be read from ``ads_performance_daily`` at request time**, which is the obvious
implementation and is wrong: that table keeps 60 days while this one keeps 90, so a 90-day window
would silently be SP-only for its first third.

── Why ``ad_spend`` is NOT simply updated in place ──

``ad_spend`` and ``net_proceeds`` stay as AMAZON reported them, and the SB figure is added on READ.
That keeps these rows a faithful cache of what Amazon actually said, so changing the attribution
basis later is a recompute rather than a refetch — and a bug in the allocator cannot corrupt the SP
figures, which currently reconcile against the Advertising API to −0.0%.

``server_default="0"`` rather than nullable: every pre-existing row genuinely has zero SB spend
attributed to it, which is a real fact about the data. That is the ``from_stock`` precedent, and
deliberately NOT the ``over_pack_approved_units`` one — there, NULL had to mean "nobody has decided",
which is a different thing from zero.

``sb_basis`` records HOW each row's figure was derived (``"declared"`` from Amazon's own ASIN list,
``"spread"`` from the brand-level remainder, ``""`` for untouched rows) so the two can be told apart
later without re-running the allocator. It is never shown on screen — the instruction was explicit:
*"no separate labelling of it in the portfolio tab. just add to the main ad figures of each product."*

Revision ID: b91d4a7c3e26
Revises: e7b3f0c92a41
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = "b91d4a7c3e26"
down_revision = "e7b3f0c92a41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `batch_alter_table` because SQLite cannot ALTER a column in place. A harmless no-op on
    # Postgres — `PostgresqlImpl.requires_recreate_in_batch` returns False unconditionally — which
    # is what keeps the deferred Postgres move unaffected by this.
    with op.batch_alter_table("economics_daily") as batch:
        batch.add_column(
            sa.Column(
                "sb_spend",
                sa.Numeric(12, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "sb_basis",
                sa.String(12),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("economics_daily") as batch:
        batch.drop_column("sb_basis")
        batch.drop_column("sb_spend")
