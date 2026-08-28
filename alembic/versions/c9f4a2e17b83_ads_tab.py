"""ads tab: entity cache, per-window performance, saved rules, mutation ledger

The Ads tab is **the first feature in this app that writes to Amazon.** Everything before it reads
Amazon and writes only our own records; a rule run here changes live bids, and therefore live spend.
Three of these four tables exist to make that safe rather than to make it possible.

ads_entity       — campaigns, ad groups, keywords and targeting clauses in ONE table, because the
                   tab treats them as a hierarchy to walk and a bid to edit. Never fully populated:
                   measured 148,291 keywords and 200,000+ targeting clauses on this account, over
                   9 minutes to page, so only rows a report or a rule has touched are cached.
ads_performance  — spend/sales per (window, entity). Separate from ads_entity because the same
                   keyword has a 7-day and a 30-day figure and both are valid at once. **These rows
                   are the working set for a rule**: the report returns only entities with activity
                   (12,854 rows for a 7-day window), and a bid rule can only act on those.
ads_rule         — saved conditions + action, as JSON. A convenience, NOT a schedule: nothing in
                   this app runs a rule automatically.
ads_mutation     — every bid change with the value it had BEFORE, written before the request is
                   sent. This is the table that makes bulk editing reversible.

Why `old_bid` is not optional: a single rule matched 299 rows carrying Rs 102,945 of weekly spend on
this account, and Amazon has no undo. Without the previous value, reversing a mistaken run means
reading 299 numbers off a report that has already moved on.

Why `status` and `error` rather than a boolean: `PUT /sp/keywords` answers **207 Multi-Status** with
separate `success` and `error` arrays — measured — so partial failure is the normal case, not the
exception. A bid below the marketplace minimum fails for that row alone while the rest succeed.

No data migration: all four tables start empty. Nothing existing is altered, so this migration
cannot invalidate a row that is already there.

Revision ID: c9f4a2e17b83
Revises: b8e3f1a67c94
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c9f4a2e17b83"
down_revision: Union[str, None] = "b8e3f1a67c94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── ads_entity ───────────────────────────────────────────────────────────
    #
    # `entity_id` is TEXT although Amazon's ids are 15-digit numbers: Amazon documents them as
    # opaque strings, and a numeric column would lose a leading zero or choke on a future
    # non-numeric id. Verified the keyword and target id spaces do not collide (0 overlaps across a
    # 1,000-id sample), so (entity_type, entity_id) is a safe unique key.
    op.create_table(
        "ads_entity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=12), nullable=False),
        sa.Column("entity_id", sa.String(length=32), nullable=False),
        sa.Column("parent_id", sa.String(length=32), nullable=True),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("state", sa.String(length=12), nullable=True),
        # What decides which endpoint a write goes to. The report labels both id columns
        # `keywordId`, so without this a targetId can be sent to /sp/keywords and fail silently
        # inside a 207.
        sa.Column("match_type", sa.String(length=40), nullable=True),
        # NULL means "inherits the ad group default", which is NOT the same as 0.0 — writing a bid
        # onto an inheriting target converts it to fixed.
        sa.Column("bid", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("default_bid", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("daily_budget", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_ads_entity_type_id", "ads_entity", ["entity_type", "entity_id"], unique=True
    )
    op.create_index("idx_ads_entity_parent", "ads_entity", ["entity_type", "parent_id"])
    op.create_index("idx_ads_entity_campaign", "ads_entity", ["campaign_id"])

    # ── ads_performance ──────────────────────────────────────────────────────
    #
    # ROAS and ACOS are deliberately ABSENT: they are derived in `ads.logic` from spend and sales.
    # A stored ratio disagrees with its own numerator the moment either input is corrected.
    op.create_table(
        "ads_performance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(length=10), nullable=False),
        sa.Column("window_end", sa.String(length=10), nullable=False),
        sa.Column("entity_id", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=12), nullable=False,
                  server_default="target"),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("ad_group_id", sa.String(length=32), nullable=True),
        sa.Column("text", sa.String(length=500), nullable=True),
        sa.Column("match_type", sa.String(length=40), nullable=True),
        # The bid as REPORTED for the window — not authoritative for a write, because a bid edited
        # in Seller Central since the report would be stale here.
        sa.Column("reported_bid", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("spend", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("orders", sa.Integer(), nullable=True),
        sa.Column("sales", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_ads_perf_window_entity", "ads_performance",
        ["window_start", "window_end", "entity_id"], unique=True,
    )
    op.create_index(
        "idx_ads_perf_window_campaign", "ads_performance",
        ["window_start", "window_end", "campaign_id"],
    )

    # ── ads_rule ─────────────────────────────────────────────────────────────
    op.create_table(
        "ads_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        # JSON for the same reason portfolio_settings is: the rule vocabulary is the part of this
        # feature most likely to grow, and a new field would otherwise need a migration.
        sa.Column("conditions_json", sa.Text(), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("window_days", sa.Integer(), nullable=True, server_default="7"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ads_rule_name", "ads_rule", ["name"], unique=True)

    # ── ads_mutation ─────────────────────────────────────────────────────────
    #
    # The audit trail, and the undo. `old_bid` is written BEFORE the request is sent, so a crash
    # mid-run leaves `pending` rows that name exactly what was in flight.
    op.create_table(
        "ads_mutation",
        sa.Column("id", sa.Integer(), nullable=False),
        # uuid4 minted per Apply rather than an autoincrement: every row of a run must carry it
        # before any row is written, and it appears in the URL of an undo.
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("entity_id", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=12), nullable=False,
                  server_default="keyword"),
        # Which ENDPOINT this row was sent to, recorded rather than re-derived so a misrouted write
        # is visible in the ledger after the fact.
        sa.Column("writer", sa.String(length=12), nullable=False, server_default="keyword"),
        sa.Column("text", sa.String(length=500), nullable=True),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("ad_group_id", sa.String(length=32), nullable=True),
        sa.Column("old_bid", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("new_bid", sa.Numeric(precision=12, scale=2), nullable=True),
        # pending | applied | failed | reverted — three real outcomes plus in-flight, because 207
        # Multi-Status makes partial failure normal.
        sa.Column("status", sa.String(length=12), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("rule_summary", sa.String(length=300), nullable=True),
        sa.Column("reverts_run_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_ads_mutation_run", "ads_mutation", ["run_id"])
    op.create_index("idx_ads_mutation_entity", "ads_mutation", ["entity_id"])
    op.create_index(
        "idx_ads_mutation_run_entity", "ads_mutation", ["run_id", "entity_id"], unique=True
    )


def downgrade() -> None:
    # Dropped in reverse creation order. There are no foreign keys between these tables — a
    # mutation deliberately does not reference `ads_entity`, because the entity cache is prunable
    # and the audit trail must outlive it.
    op.drop_index("idx_ads_mutation_run_entity", table_name="ads_mutation")
    op.drop_index("idx_ads_mutation_entity", table_name="ads_mutation")
    op.drop_index("idx_ads_mutation_run", table_name="ads_mutation")
    op.drop_table("ads_mutation")

    op.drop_index("idx_ads_rule_name", table_name="ads_rule")
    op.drop_table("ads_rule")

    op.drop_index("idx_ads_perf_window_campaign", table_name="ads_performance")
    op.drop_index("idx_ads_perf_window_entity", table_name="ads_performance")
    op.drop_table("ads_performance")

    op.drop_index("idx_ads_entity_campaign", table_name="ads_entity")
    op.drop_index("idx_ads_entity_parent", table_name="ads_entity")
    op.drop_index("idx_ads_entity_type_id", table_name="ads_entity")
    op.drop_table("ads_entity")
