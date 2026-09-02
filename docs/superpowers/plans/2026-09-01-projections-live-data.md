# Projections: active-parent filtering, live sales, 7d/30d weighted blend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Projections tab's static 81-parent list with one derived live from the MRP
sheet (same source and fallback chain the Shipment tab uses), replace the manual CSV upload with
already-stored Amazon economics data, and blend a 7-day and 30-day sales rate so the forecast
responds to real spikes and drops without being dragged to zero by a single quiet week.

**Architecture:** A new `app/projections/` package (`logic.py` pure, `repository.py` the only SQL,
following the `app/ads/` module split) sits beside the existing router. `app/routers/projections.py`
gains a DB dependency it does not have today. One new table, `ProjectionRow`, keyed on parent
product NAME (like `ProductRawStock`), holds both the purchasing config and the computed sales
rate per parent, with a `source` column so a hand-typed override survives the next scheduled
recompute. A second new table, `ProjectionRefresh`, records each weekly run (mirrors
`EconomicsRefresh`). The weekly job reuses `app/portfolio/economics.fetch_economics` and
`app/portfolio/repository.save_snapshot`/`load_snapshot` — no new Amazon integration.

**Tech Stack:** FastAPI + SQLAlchemy async + SQLite/PostgreSQL (existing), Alembic, APScheduler,
Jinja2 + vanilla JS (existing `templates/projections.html`), pytest + pytest-asyncio (existing).

## Global Constraints

- **Money/weight/rate values as `Numeric`; every caller converts to `float` before returning JSON.**
  `Decimal` reaching `JSONResponse` is a 500 — this app has shipped that defect twice already.
- **`units_ordered`, never `net_units`**, as the sales-demand input. `net_units` goes negative on
  refund-heavy weeks (measured: 2 ASINs in the live 7-day window).
- **A parent's grouping unit is the MRP sheet's own `name` field, unmerged** — never
  `product_families.json`, never Portfolio's `family_label()` (that merges flavours for a display
  rollup; this is a purchasing decision, and flavours are separate purchase decisions).
- **A hidden/excluded row is always named, capped at 8, never a bare count.**
- **Every setting (`projection_blend`) is range-checked on READ and on WRITE.** The `good_rating: 99`
  lesson: a stored-but-unchecked value silently breaks every row that reads it.
- **A scheduled job never writes to Amazon and never overwrites a hand-edited row.**
- **Every new scheduled time is stated in IST and converted through `app.ist.utc_hhmm`** — never a
  bare hour passed to `CronTrigger`. This is the sixth-bug lesson; asserting the IST constant, never
  the derived UTC one, in any test.
- **`require_auth` stays on every existing and new `/projections/*` route** — permission-area
  migration is out of scope for this change.
- Every new migration gets a newest-first branch in `deploy/update-ec2.sh`'s baseline detector, and
  the new table added to its required-tables check. A stale detector has stamped production
  backwards before.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/projections/__init__.py` | new, empty — package marker |
| `app/projections/logic.py` | new, pure: parent-row assembly from sheet+defaults, sales blend math, settings validation. No DB, no network. |
| `app/projections/repository.py` | new, only SQL: `ProjectionRow` and `ProjectionRefresh` CRUD |
| `app/projections/refresh.py` | new: the weekly job body — fetch/reuse economics windows, recompute, record the run |
| `app/models.py` | modified: add `ProjectionRow`, `ProjectionRefresh` |
| `alembic/versions/<new>_projection_rows_and_refresh.py` | new migration |
| `app/routers/projections.py` | modified: DB dependency added; routes rebuilt on `app/projections/*` |
| `app/scheduler.py` | modified: `PROJECTIONS_REFRESH_IST`, `scheduled_projections_refresh`, registration |
| `deploy/update-ec2.sh` | modified: detector branch + required-tables entry |
| `templates/projections.html` | modified: sales-source column, needs-review band, hidden-parents note, blend settings panel |
| `tests/test_projections_logic.py` | new |
| `tests/test_projections_repository.py` | new |
| `tests/test_projections_api.py` | new |
| `CLAUDE.md` | modified: new section documenting the change |

---

## Task 1: `ProjectionRow` and `ProjectionRefresh` models + migration

**Files:**
- Modify: `app/models.py` (append after `class EconomicsRefresh` block, i.e. after line ~837)
- Create: `alembic/versions/<new>_projection_rows_and_refresh.py`
- Test: `tests/test_schema_migrations.py` (existing file — add one assertion, see Step 6)

**Interfaces:**
- Produces: `ProjectionRow` (columns below) and `ProjectionRefresh` (mirrors `EconomicsRefresh`
  exactly: `id, window_start, window_end, rows_stored, error, started_at, finished_at`), importable
  as `from app.models import ProjectionRow, ProjectionRefresh`.

- [ ] **Step 1: Add the two model classes to `app/models.py`**

Insert immediately after the existing `EconomicsRefresh` class (ends at the line with
`finished_at = Column(DateTime)` around line 837, before the blank lines preceding
`ProductDecision`):

```python
class ProjectionRow(Base):
    """One parent product's purchasing forecast row. **Keyed on the parent product NAME, not an
    ASIN** — the same choice `ProductRawStock` makes, for the same reason: this is a purchasing
    decision taken at the parent level, and the MRP sheet's own `name` column is the only stable
    identifier a genuinely new product (Triphala Sattu) carries from day one.

    **`source` is what lets a hand-typed override survive a scheduled recompute.** The weekly job
    only overwrites `sales_source == "sheet"` rows; a `"manual"` row is left alone until the owner
    explicitly resets it. Without this column a refresh would silently discard a manual correction
    the next time it ran — the same failure `ProductDecision` avoids by never being touched by an
    automated pass at all.

    **`needs_review` is set when no entry in `projection_defaults.json` matched this parent's name**
    (case/space/hyphen-insensitive). The row still gets Global Defaults so it is never invisible —
    invisible-because-unconfigured is exactly the Triphala Sattu bug this whole change removes —
    but the owner is told to check the purchase rate and lead times rather than trusting a global
    guess silently.
    """
    __tablename__ = "projection_row"
    __table_args__ = (
        Index("idx_projection_row_parent", "parent_product", unique=True),
    )

    id = Column(Integer, primary_key=True)
    #: The MRP sheet's own product name, unmerged — see the class docstring.
    parent_product = Column(String(120), nullable=False)
    brand = Column(String(60))

    # ── Purchasing config, matched from projection_defaults.json or Global Defaults ──
    purchase_rate = Column(Numeric(10, 2), default=0)
    supplier_to_wh = Column(Integer, default=5)
    packing = Column(Integer, default=2)
    wh_to_ixd = Column(Integer, default=10)
    ixd_to_fba = Column(Integer, default=5)
    wh_buffer_days = Column(Numeric(6, 1), default=10)
    seasonal_impact = Column(Numeric(6, 2), default=1.0)
    growth_rate = Column(Numeric(6, 2), default=0.3)
    #: True when no `projection_defaults.json` entry matched this parent's name. See class docstring.
    needs_review = Column(Boolean, default=False, nullable=False)

    # ── Sales, from economics_snapshot or a manual edit ──
    #: "sheet" (from units_ordered x weight) or "manual" (hand-typed). A "manual" row is skipped by
    #: the weekly recompute.
    sales_source = Column(String(10), nullable=False, default="sheet")
    last_month_sale = Column(Numeric(10, 2), default=0)
    #: kg/day from the LAST 7 DAYS of units_ordered x weight. NULL, never 0.0, when no 7-day
    #: snapshot exists yet for this parent — distinct from a genuine zero-sales week, which is a
    #: real 0.0 and IS blended. See `app.projections.logic.blended_daily_rate`.
    seven_day_rate = Column(Numeric(10, 2))
    #: kg/day from the last 30 days. Always populated once any sheet-sourced row is computed.
    thirty_day_rate = Column(Numeric(10, 2))
    #: The blended rate actually used for the forecast — what `calculate_projections` reads.
    daily_rate = Column(Numeric(10, 2), default=0)
    #: True when |seven_day_rate/thirty_day_rate - 1| exceeded the saved divergence threshold at
    #: the last recompute, so the screen can show WHY a number moved.
    diverged = Column(Boolean, default=False, nullable=False)

    # ── Owner-entered stock and current values, unaffected by the sales source ──
    current_fba_stock = Column(Numeric(10, 1), default=0)
    current_wh_stock = Column(Numeric(10, 1), default=0)

    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(60))


class ProjectionRefresh(Base):
    """When the weekly 7d/30d sales blend was last recomputed, and what it covered. One row per
    run — the same shape as `EconomicsRefresh`, so "the numbers stopped updating" is answerable
    the same way on this tab as on the Portfolio tab.
    """
    __tablename__ = "projection_refresh"

    id = Column(Integer, primary_key=True)
    window_start = Column(String(10))
    window_end = Column(String(10))
    rows_stored = Column(Integer, default=0)
    error = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
```

Confirm `Boolean` is already imported at the top of `app/models.py` (it is — `AdsRefresh` and
several other classes already use `Column(Boolean, ...)`); if for any reason it is not in the
`from sqlalchemy import (...)` block, add it there.

- [ ] **Step 2: Generate the migration**

Run: `venv/Scripts/python -m alembic revision -m "projection rows and refresh"`

Note the generated revision id (a 12-char hex string) — call it `<REV>` for the rest of this task.
Confirm the current head first:

Run: `venv/Scripts/python -m alembic heads`
Expected: `c5e91a3d47b6 (head)` — this becomes `down_revision`.

- [ ] **Step 3: Write the migration body**

Open the generated file at `alembic/versions/<REV>_projection_rows_and_refresh.py` and replace its
contents with:

```python
"""projections: parent rows (name-keyed) and a weekly-refresh record

**Additive only** — two new tables, nothing existing touched, so this is safe on a populated
database and the downgrade is exact.

`projection_row` replaces `app/invoice/projection_defaults.json` and `projection_data.json` (a
flat file at repo root) as the source of truth for the Projections tab: keyed on the parent
product NAME (matching `product_raw_stock`'s own choice, for the same reason — this is bulk
purchasing, and the MRP sheet's name is the one identifier a brand-new product carries from day
one). `sales_source` is what lets a hand-typed sales override survive the weekly recompute that
`projection_refresh` records the history of.

Revision ID: <REV>
Revises: c5e91a3d47b6
Create Date: 2026-09-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "<REV>"
down_revision: Union[str, None] = "c5e91a3d47b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projection_row",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("parent_product", sa.String(length=120), nullable=False),
        sa.Column("brand", sa.String(length=60), nullable=True),
        sa.Column("purchase_rate", sa.Numeric(10, 2), nullable=True, server_default="0"),
        sa.Column("supplier_to_wh", sa.Integer(), nullable=True, server_default="5"),
        sa.Column("packing", sa.Integer(), nullable=True, server_default="2"),
        sa.Column("wh_to_ixd", sa.Integer(), nullable=True, server_default="10"),
        sa.Column("ixd_to_fba", sa.Integer(), nullable=True, server_default="5"),
        sa.Column("wh_buffer_days", sa.Numeric(6, 1), nullable=True, server_default="10"),
        sa.Column("seasonal_impact", sa.Numeric(6, 2), nullable=True, server_default="1.0"),
        sa.Column("growth_rate", sa.Numeric(6, 2), nullable=True, server_default="0.3"),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sales_source", sa.String(length=10), nullable=False, server_default="sheet"),
        sa.Column("last_month_sale", sa.Numeric(10, 2), nullable=True, server_default="0"),
        sa.Column("seven_day_rate", sa.Numeric(10, 2), nullable=True),
        sa.Column("thirty_day_rate", sa.Numeric(10, 2), nullable=True),
        sa.Column("daily_rate", sa.Numeric(10, 2), nullable=True, server_default="0"),
        sa.Column("diverged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("current_fba_stock", sa.Numeric(10, 1), nullable=True, server_default="0"),
        sa.Column("current_wh_stock", sa.Numeric(10, 1), nullable=True, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(length=60), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_projection_row_parent", "projection_row", ["parent_product"], unique=True
    )

    op.create_table(
        "projection_refresh",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.String(length=10), nullable=True),
        sa.Column("window_end", sa.String(length=10), nullable=True),
        sa.Column("rows_stored", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("projection_refresh")
    op.drop_index("idx_projection_row_parent", table_name="projection_row")
    op.drop_table("projection_row")
```

Replace both `<REV>` placeholders (the `revision` line and the header comment) with the actual
generated id from Step 2.

- [ ] **Step 4: Run the migration locally**

Run: `venv/Scripts/python -m alembic upgrade head`
Expected: output ends with `Running upgrade c5e91a3d47b6 -> <REV>, projections: parent rows...`
and no error.

Run: `venv/Scripts/python -m alembic current`
Expected: `<REV> (head)`

- [ ] **Step 5: Verify the tables and unique index exist**

Run:
```bash
venv/Scripts/python -c "
import sqlite3
con = sqlite3.connect('tracker.db')
tables = {r[0] for r in con.execute(\"select name from sqlite_master where type='table'\")}
assert 'projection_row' in tables and 'projection_refresh' in tables, tables
idx = {r[1] for r in con.execute('PRAGMA index_list(\"projection_row\")')}
assert 'idx_projection_row_parent' in idx, idx
print('OK')
"
```
Expected: `OK`

- [ ] **Step 6: Add a schema-migration test**

Open `tests/test_schema_migrations.py`. Find the test that runs the deploy detector heredoc (search
for `BASELINE=` or `def test_the_deploy_detector` — the exact function asserting the detector
answers the true head against a freshly migrated database). Do not modify that test yet — Task 7
adds the detector branch it needs. For now add a standalone new test in the same file:

```python
async def test_projection_tables_exist_after_migration(db):
    """The two new tables are reachable through the ORM, not just through raw SQL — catches a
    migration that runs but a model whose columns don't match it."""
    from sqlalchemy import select

    from app.models import ProjectionRefresh, ProjectionRow

    await db.execute(select(ProjectionRow))
    await db.execute(select(ProjectionRefresh))
```

- [ ] **Step 7: Run it**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py -q -p no:randomly`
Expected: all pass, including the new test.

- [ ] **Step 8: Commit**

```bash
git add app/models.py "alembic/versions/<REV>_projection_rows_and_refresh.py" tests/test_schema_migrations.py
git commit -m "feat(projections): add projection_row and projection_refresh tables"
```

---

## Task 2: `app/projections/logic.py` — parent-name grouping and defaults matching

**Files:**
- Create: `app/projections/__init__.py` (empty file)
- Create: `app/projections/logic.py`
- Test: `tests/test_projections_logic.py`

**Interfaces:**
- Consumes: nothing from other tasks — this module is pure (no DB, no network), matching every
  other feature's `logic.py` in this codebase.
- Produces (used by Task 4's repository and Task 5's router):
  - `normalize_name(name: str) -> str`
  - `match_defaults(parent_name: str, defaults: dict) -> dict | None`
  - `group_active_by_name(catalogue: dict[str, dict]) -> dict[str, dict]`
  - `build_parent_config(parent_name: str, group: dict, defaults: dict, global_defaults: dict) -> dict`
  - `calculate_projections(products: list[dict]) -> list[dict]` (moved here verbatim from
    `app/routers/projections.py`, unchanged — see Step 5)

- [ ] **Step 1: Create the package marker**

Run: `New-Item -ItemType File -Path "app/projections/__init__.py"` (or on a Unix shell:
`touch app/projections/__init__.py`). Confirm the directory `app/projections/` did not previously
exist:

Run: `venv/Scripts/python -c "import app.projections"`
Expected: no output, no error (empty package imports cleanly).

- [ ] **Step 2: Write the failing tests for name normalization and defaults matching**

Create `tests/test_projections_logic.py`:

```python
"""Pure rules for the Projections tab. No database, no network.

The parent-grouping unit is the MRP sheet's own product `name`, UNMERGED — never
`product_families.json`, and never Portfolio's `family_label()`. That function collapses flavour
variants (Cheese & Cream Chana, Nimbu Pudina Chana, Peri Peri Chana...) into one shared display
name for a rollup; `product_families.json` itself keeps them as separate parents, because
different flavours are different recipes and different purchase decisions. Folding them here
would be wrong for exactly the reason `family_label` is right for Portfolio's screen.
"""
from app.projections import logic


# ─── normalize_name ───────────────────────────────────────────────────────────


def test_normalize_name_ignores_case_space_and_hyphen():
    """`projection_defaults.json` spells "Govind Bhog Rice" with a space; other data sources
    may not agree on spacing or case, so matching must be forgiving about exactly these three
    things and nothing else — a genuine spelling difference (Gobindobhog vs Govind Bhog) must
    still NOT match, or two different products merge into one row."""
    assert logic.normalize_name("Govind Bhog Rice") == logic.normalize_name("govind-bhog rice")
    assert logic.normalize_name("Chana Sattu") == logic.normalize_name("CHANA SATTU")
    assert logic.normalize_name("Govind Bhog Rice") != logic.normalize_name("Gobindobhog Rice")


def test_normalize_name_handles_empty_and_none():
    assert logic.normalize_name("") == ""
    assert logic.normalize_name(None) == ""


# ─── match_defaults ───────────────────────────────────────────────────────────


def test_match_defaults_finds_a_normalized_match():
    defaults = {"Govind Bhog Rice": {"purchase_rate": 75.0}}
    assert logic.match_defaults("govind-bhog rice", defaults) == {"purchase_rate": 75.0}


def test_match_defaults_returns_none_for_a_genuinely_new_parent():
    """Triphala Sattu-shaped: active in the sheet, no entry anywhere in
    projection_defaults.json under any spelling. The caller (build_parent_config) is what
    turns this None into Global Defaults + needs_review — this function must not guess."""
    defaults = {"Govind Bhog Rice": {"purchase_rate": 75.0}}
    assert logic.match_defaults("Triphala Sattu", defaults) is None


# ─── group_active_by_name ─────────────────────────────────────────────────────


def _sheet_row(asin, name, weight, active=True, brand="Mithila Foods"):
    return {"asin": asin, "name": name, "weight": weight, "brand": brand, "active": active}


def test_group_active_by_name_excludes_inactive_asins():
    catalogue = {
        "B0ACTIVE01": _sheet_row("B0ACTIVE01", "Chana Sattu", 0.5),
        "B0DEAD0001": _sheet_row("B0DEAD0001", "Kasundi", 0.3, active=False),
    }
    groups = logic.group_active_by_name(catalogue)
    assert list(groups) == ["Chana Sattu"]


def test_group_active_by_name_keeps_flavours_as_separate_parents():
    """The measured case: Cheese & Cream Roasted Chana and Peri Peri Roasted Chana are two
    ASINs of the SAME flavour-suffix pattern but must stay two separate groups — this function
    does no flavour merging at all, by design."""
    catalogue = {
        "B0CHEESE01": _sheet_row("B0CHEESE01", "Cheese & Cream Roasted Chana", 0.2),
        "B0PERI0001": _sheet_row("B0PERI0001", "Peri Peri Roasted Chana", 0.2),
    }
    groups = logic.group_active_by_name(catalogue)
    assert set(groups) == {"Cheese & Cream Roasted Chana", "Peri Peri Roasted Chana"}


def test_group_active_by_name_collects_every_pack_size_asin():
    catalogue = {
        "B0SIZE0001": _sheet_row("B0SIZE0001", "Chana Sattu", 0.5),
        "B0SIZE0002": _sheet_row("B0SIZE0002", "Chana Sattu", 1.0),
        "B0SIZE0003": _sheet_row("B0SIZE0003", "Chana Sattu", 2.0),
    }
    groups = logic.group_active_by_name(catalogue)
    assert sorted(groups["Chana Sattu"]["asins"]) == ["B0SIZE0001", "B0SIZE0002", "B0SIZE0003"]
    assert groups["Chana Sattu"]["weights"] == {
        "B0SIZE0001": 0.5, "B0SIZE0002": 1.0, "B0SIZE0003": 2.0,
    }


def test_group_active_by_name_is_empty_for_an_empty_catalogue():
    assert logic.group_active_by_name({}) == {}


# ─── build_parent_config ──────────────────────────────────────────────────────

GLOBAL_DEFAULTS = {
    "growth_rate": 0.3, "seasonal_impact": 1.0, "supplier_to_wh": 5, "packing": 2,
    "wh_to_ixd": 10, "ixd_to_fba": 5, "wh_buffer_days": 10.0,
}


def test_build_parent_config_uses_matched_defaults():
    defaults = {"Chana Sattu": {"purchase_rate": 120.0, "supplier_to_wh": 5, "packing": 2,
                                 "wh_to_ixd": 10, "ixd_to_fba": 5, "wh_buffer_days": 10.0,
                                 "seasonal_impact": 1.5, "growth_rate": 0.3, "brand": "Mithila Foods"}}
    config = logic.build_parent_config("Chana Sattu", {}, defaults, GLOBAL_DEFAULTS)
    assert config["purchase_rate"] == 120.0
    assert config["needs_review"] is False


def test_build_parent_config_flags_needs_review_with_global_defaults():
    """The Triphala Sattu case: no match anywhere, so it gets Global Defaults and is flagged —
    never hidden, per the owner's explicit decision that a live product must never be invisible
    because a static file has not heard of it."""
    config = logic.build_parent_config("Triphala Sattu", {}, {}, GLOBAL_DEFAULTS)
    assert config["needs_review"] is True
    assert config["purchase_rate"] == 0
    assert config["seasonal_impact"] == GLOBAL_DEFAULTS["seasonal_impact"]
    assert config["growth_rate"] == GLOBAL_DEFAULTS["growth_rate"]
    assert config["wh_buffer_days"] == GLOBAL_DEFAULTS["wh_buffer_days"]


# ─── calculate_projections: the blended rate must actually be used ────────────


def test_calculate_projections_forecasts_from_the_blended_rate_for_a_sheet_row():
    """**The bug this test exists to catch: the weekly blend must not be silently discarded.**

    A `sheet`-sourced row already carries `daily_rate` from `blended_daily_rate` — computed once
    a week from real units_ordered data. Re-deriving `daily_rate` from `last_month_sale *
    seasonal * (1 + growth)` here, as the pre-existing formula did unconditionally, would make
    the entire 7d/30d blend (the whole reason this feature exists) invisible on screen: the
    number the weekly job computes and the number the forecast displays would disagree.
    """
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "sheet",
        "daily_rate": 14.0,               # the blended rate, as Task 6's refresh job would store it
        "last_month_sale": 300.0,          # 30-day total kg — NOT what the forecast should use
        "seasonal_impact": 2.0, "growth_rate": 5.0,   # deliberately extreme, to prove they are IGNORED
        "purchase_rate": 0, "supplier_to_wh": 0, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(products)[0]
    assert result["daily_rate"] == 14.0, "the blended rate was overwritten"
    assert result["monthly_forecast"] == 420.0, "monthly_forecast must be daily_rate * 30"


def test_calculate_projections_falls_back_to_last_month_sale_for_a_manual_row():
    """A `manual` row never went through the weekly job and has no blended rate to read — it
    must keep using the original seasonal/growth formula, unchanged from before this feature."""
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "manual",
        "daily_rate": 0, "last_month_sale": 300.0,
        "seasonal_impact": 1.5, "growth_rate": 0.2,
        "purchase_rate": 0, "supplier_to_wh": 0, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(products)[0]
    # 300 * 1.5 * 1.2 = 540
    assert result["monthly_forecast"] == 540.0
    assert result["daily_rate"] == pytest.approx(18.0)


def test_calculate_projections_falls_back_for_a_sheet_row_never_yet_refreshed():
    """A brand-new parent (`build_current_rows` just created it) is `sales_source="sheet"` but
    has never been through the weekly job, so `daily_rate` is 0/None — it must fall back to the
    same formula a manual row uses, not silently forecast zero."""
    products = [{
        "parent_product": "Triphala Sattu", "sales_source": "sheet",
        "daily_rate": 0, "last_month_sale": 30.0,
        "seasonal_impact": 1.0, "growth_rate": 0.3,
        "purchase_rate": 0, "supplier_to_wh": 0, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(products)[0]
    assert result["monthly_forecast"] == pytest.approx(39.0)  # 30 * 1.0 * 1.3
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly`
Expected: `ModuleNotFoundError: No module named 'app.projections.logic'` or collection error —
the module does not exist yet.

- [ ] **Step 4: Write `app/projections/logic.py`**

```python
"""Pure rules for the Projections tab. No database, no network.

**The parent-grouping unit is the MRP sheet's own product `name`, unmerged.** Never
`app/invoice/product_families.json` — that static file is what left Triphala Sattu invisible
here in the first place, active in the sheet in two pack sizes and never added to the file.
And never `app.portfolio.logic.family_label()`: that function exists to give a multi-flavour
PARENT one shared display name for a Portfolio rollup, and `product_families.json` itself
keeps flavour variants (Cheese & Cream Chana, Nimbu Pudina Chana, Peri Peri Chana...) as
separate parents — different flavours are different recipes and different purchase decisions.
"""
from __future__ import annotations

import re
from collections.abc import Mapping


def normalize_name(name: str | None) -> str:
    """Case, space and hyphen insensitive — nothing more.

    Matches "Govind Bhog Rice" (projection_defaults.json's spelling) against
    "govind-bhog rice" (a plausible sheet spelling), but must NOT match "Gobindobhog Rice"
    against "Govind Bhog Rice" — those are different enough spellings that a false match would
    silently merge two products' purchasing config. `re.sub` strips exactly space and hyphen;
    it does not touch any other character, so a genuine spelling difference still differs.
    """
    if not name:
        return ""
    return re.sub(r"[\s-]+", "", name).casefold()


def match_defaults(parent_name: str, defaults: Mapping[str, dict]) -> dict | None:
    """The `projection_defaults.json` entry for this parent, matched by normalized name, or
    `None` if nothing matches — a genuinely new parent (Triphala Sattu) has no entry under any
    spelling. Returning `None` rather than a default dict here is deliberate: the caller,
    `build_parent_config`, is the one place that decides what a non-match means.
    """
    target = normalize_name(parent_name)
    if not target:
        return None
    for name, config in defaults.items():
        if normalize_name(name) == target:
            return config
    return None


def group_active_by_name(catalogue: Mapping[str, dict]) -> dict[str, dict]:
    """`{parent_name: {asins: [...], weights: {asin: weight}, brand: str}}` for every ACTIVE
    ASIN in the sheet's catalogue, grouped by its own `name` field — unmerged, see the module
    docstring. `catalogue` is `load_catalogue()`'s first return value:
    `{asin: {name, weight, brand, active}}`.
    """
    groups: dict[str, dict] = {}
    for asin, row in catalogue.items():
        if not row.get("active"):
            continue
        name = row.get("name") or ""
        if not name:
            continue
        group = groups.setdefault(name, {"asins": [], "weights": {}, "brand": row.get("brand") or ""})
        group["asins"].append(asin)
        group["weights"][asin] = row.get("weight") or 0
    return groups


#: The purchasing-config fields a matched (or Global Defaults) entry supplies. Kept as a tuple so
#: `build_parent_config` and its test cannot drift about which fields exist.
CONFIG_FIELDS = (
    "purchase_rate", "supplier_to_wh", "packing", "wh_to_ixd", "ixd_to_fba",
    "wh_buffer_days", "seasonal_impact", "growth_rate",
)


def build_parent_config(
    parent_name: str, group: Mapping, defaults: Mapping[str, dict], global_defaults: Mapping,
) -> dict:
    """The purchasing config for one live parent: matched saved values, or Global Defaults
    flagged `needs_review`.

    **Never hides a parent for lack of config.** A live product with no matching entry gets
    Global Defaults rather than being dropped — the Triphala Sattu bug this whole change exists
    to fix was exactly a live product being invisible because a static file had no opinion about
    it, and repeating that here with a different static file would be the same mistake twice.
    """
    matched = match_defaults(parent_name, defaults)
    config: dict = {"parent_product": parent_name, "brand": group.get("brand") or "",
                     "needs_review": matched is None}
    source = matched if matched is not None else global_defaults
    for field in CONFIG_FIELDS:
        config[field] = source.get(field, global_defaults.get(field, 0))
    return config


def calculate_projections(products: list[dict]) -> list[dict]:
    """Run projection formulas on each product row.

    **Moved from `app/routers/projections.py`, and the daily-rate source is NOT unchanged —
    this is the one piece of arithmetic this feature actually exists to change.** The
    pre-existing formula derived `daily_rate` from `last_month_sale * seasonal * (1 + growth)`
    every time, which would have silently discarded the whole 7d/30d blend: a `sheet`-sourced row
    already carries its blended `daily_rate`, computed once a week by
    `app.projections.refresh.run` from real units_ordered data, and recomputing it here from
    `last_month_sale` alone would have made Task 3's and Task 6's entire purpose invisible on
    screen.

    So: a row whose `sales_source == "sheet"` and already has a non-null `daily_rate` (the
    normal case after at least one weekly refresh) keeps that rate and derives
    `monthly_forecast = daily_rate * 30` FROM it — seasonal/growth are not applied a second
    time, because `blended_daily_rate` has no notion of them and double-applying a growth factor
    on top of a rate already measured from real sales would inflate the forecast for no reason.
    A `manual` row, or a `sheet` row that has never been refreshed yet (`daily_rate` is 0 or
    `None`, e.g. immediately after `build_current_rows` creates a brand-new parent), falls back
    to the original `last_month_sale`-driven formula, since a manual edit only ever supplies
    `last_month_sale` and has no blended rate to read.
    """
    for p in products:
        seasonal = p.get("seasonal_impact", 1.0) or 1.0
        growth = p.get("growth_rate", 0.3) or 0.0
        has_blended_rate = (
            p.get("sales_source") == "sheet" and (p.get("daily_rate") or 0) > 0
        )

        if has_blended_rate:
            daily_rate = p["daily_rate"]
            monthly_forecast = daily_rate * 30
        else:
            last_sale = p.get("last_month_sale", 0) or 0
            monthly_forecast = last_sale * seasonal * (1 + growth)
            daily_rate = monthly_forecast / 30

        s2w = p.get("supplier_to_wh", 5) or 0
        pack = p.get("packing", 2) or 0
        w2i = p.get("wh_to_ixd", 10) or 0
        i2f = p.get("ixd_to_fba", 5) or 0
        total_lead = s2w + pack + w2i + i2f
        wh_buffer = p.get("wh_buffer_days", 10) or 0

        ideal_fba = round(daily_rate * total_lead, 1)
        ideal_wh = round(daily_rate * wh_buffer, 1)

        current_fba = p.get("current_fba_stock", 0) or 0
        current_wh = p.get("current_wh_stock", 0) or 0

        shipment_alert = round(ideal_fba - current_fba, 1)
        reorder_alert = round(ideal_fba + ideal_wh - current_fba - current_wh, 1)

        purchase_rate = p.get("purchase_rate", 0) or 0
        ideal_stock_value = round((ideal_fba + ideal_wh) * purchase_rate, 0)
        current_stock_value = round((current_fba + current_wh) * purchase_rate, 0)

        inventory_days = round(current_fba / daily_rate, 1) if daily_rate > 0 else 0

        p["monthly_forecast"] = round(monthly_forecast, 1)
        p["daily_rate"] = round(daily_rate, 2)
        p["total_lead_time"] = total_lead
        p["ideal_fba_stock"] = ideal_fba
        p["ideal_wh_stock"] = ideal_wh
        p["shipment_alert"] = shipment_alert
        p["reorder_alert"] = reorder_alert
        p["ideal_stock_value"] = ideal_stock_value
        p["current_stock_value"] = current_stock_value
        p["inventory_days"] = inventory_days

    return products
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly`
Expected: all pass (13 tests).

- [ ] **Step 6: Commit**

```bash
git add app/projections/__init__.py app/projections/logic.py tests/test_projections_logic.py
git commit -m "feat(projections): pure grouping and defaults-matching logic"
```

---

## Task 3: sales-from-economics and the 7d/30d blend, in `app/projections/logic.py`

**Files:**
- Modify: `app/projections/logic.py` (append)
- Modify: `tests/test_projections_logic.py` (append)

**Interfaces:**
- Consumes: `group_active_by_name` output shape from Task 2 (`{parent: {asins, weights, brand}}`).
- Produces (used by Task 5's repository and Task 6's refresh job):
  - `sales_kg_by_parent(snapshot_rows: list[dict], groups: dict[str, dict]) -> dict[str, float]`
  - `blended_daily_rate(kg_30d: float, kg_7d: float | None, weight: float) -> tuple[float, bool]`
  - `DEFAULT_BLEND: dict` and `BLEND_RANGES: dict`
  - `blend_setting_error(key: str, value) -> str | None`
  - `blend_or_default(stored: Mapping | None) -> dict`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_projections_logic.py`:

```python
# ─── sales_kg_by_parent ────────────────────────────────────────────────────────


def _econ_row(asin, units_ordered, net_units=None):
    """One economics_snapshot row in Amazon's own nested shape — the same shape
    `app.portfolio.economics.fetch_economics` returns and `app.portfolio.repository.load_snapshot`
    reconstructs from storage, so this fixture is honest about what the real caller passes."""
    return {
        "childAsin": asin,
        "sales": {
            "unitsOrdered": units_ordered,
            "netUnitsSold": net_units if net_units is not None else units_ordered,
        },
    }


def test_sales_kg_by_parent_sums_units_ordered_times_weight():
    groups = {"Chana Sattu": {"asins": ["B01"], "weights": {"B01": 0.5}, "brand": ""}}
    rows = [_econ_row("B01", units_ordered=100)]
    result = logic.sales_kg_by_parent(rows, groups)
    assert result == {"Chana Sattu": 50.0}


def test_sales_kg_by_parent_sums_multiple_pack_sizes_into_one_parent():
    groups = {"Chana Sattu": {"asins": ["B01", "B02"], "weights": {"B01": 0.5, "B02": 1.0},
                              "brand": ""}}
    rows = [_econ_row("B01", units_ordered=100), _econ_row("B02", units_ordered=40)]
    result = logic.sales_kg_by_parent(rows, groups)
    assert result == {"Chana Sattu": 90.0}  # 100*0.5 + 40*1.0


def test_sales_kg_by_parent_ignores_net_units_and_never_goes_negative():
    """Measured cause: net_units went negative (-1) on 2 ASINs in a real refund-heavy 7-day
    window. units_ordered is the demand signal; a returns problem is not lower demand, and a
    negative daily rate would produce a negative purchase quantity."""
    groups = {"Chana Sattu": {"asins": ["B01"], "weights": {"B01": 1.0}, "brand": ""}}
    rows = [_econ_row("B01", units_ordered=5, net_units=-1)]
    result = logic.sales_kg_by_parent(rows, groups)
    assert result == {"Chana Sattu": 5.0}, "net_units leaked into the sales figure"


def test_sales_kg_by_parent_ignores_an_asin_outside_the_active_groups():
    """A row for a discontinued or unknown ASIN must not silently create a phantom parent."""
    groups = {"Chana Sattu": {"asins": ["B01"], "weights": {"B01": 1.0}, "brand": ""}}
    rows = [_econ_row("B01", units_ordered=10), _econ_row("B99UNKNOWN", units_ordered=999)]
    result = logic.sales_kg_by_parent(rows, groups)
    assert result == {"Chana Sattu": 10.0}


def test_sales_kg_by_parent_is_empty_for_no_rows():
    groups = {"Chana Sattu": {"asins": ["B01"], "weights": {"B01": 1.0}, "brand": ""}}
    assert logic.sales_kg_by_parent([], groups) == {}


# ─── blended_daily_rate ────────────────────────────────────────────────────────


def test_blended_daily_rate_weights_seven_and_thirty_day():
    """0.4 * (7d/7) + 0.6 * (30d/30), the default weight — verified against real account
    figures: Bangla Moori-shaped (7d rate above 30d) and Miniket-shaped (7d rate below) both
    move in the direction the blend implies, not toward zero."""
    rate, diverged = logic.blended_daily_rate(kg_30d=300.0, kg_7d=70.0, weight=0.4)
    # 30d/day = 10.0, 7d/day = 10.0 -> exact agreement, no divergence
    assert rate == 10.0
    assert diverged is False


def test_blended_daily_rate_responds_to_a_spike():
    # 30d/day = 10.0, 7d/day = 20.0 (2x) -> blended = 0.4*20 + 0.6*10 = 14.0
    rate, diverged = logic.blended_daily_rate(kg_30d=300.0, kg_7d=140.0, weight=0.4)
    assert rate == 14.0
    assert diverged is True, "a 2x week-over-month move must be flagged against the default 30% threshold"


def test_blended_daily_rate_takes_an_explicit_divergence_threshold():
    """The threshold is a PARAMETER, not a hardcoded constant, because it is a saved, editable
    setting (`DEFAULT_BLEND['divergence_pct']`) — the refresh job (Task 6) reads it from storage
    and must be able to pass a value other than the default."""
    # 30d/day = 10.0, 7d/day = 11.0 -> 10% move: not diverged at the default 30% threshold...
    _, diverged_default = logic.blended_daily_rate(kg_30d=300.0, kg_7d=77.0, weight=0.4)
    assert diverged_default is False
    # ...but IS diverged against a tight 5% threshold, passed explicitly.
    _, diverged_tight = logic.blended_daily_rate(
        kg_30d=300.0, kg_7d=77.0, weight=0.4, divergence_fraction=0.05,
    )
    assert diverged_tight is True


def test_blended_daily_rate_falls_back_to_thirty_day_when_seven_day_is_missing():
    """kg_7d=None means no 7-day snapshot exists yet for this parent — NOT a zero-sales week.
    Falling back entirely (rather than blending toward zero) is the whole point: 4 of 47
    currently-selling parents on the real account had 30-day sales but no stored 7-day window
    at all when this was measured, and treating that as a zero would have cut their forecasts
    40% on no evidence.
    """
    rate, diverged = logic.blended_daily_rate(kg_30d=300.0, kg_7d=None, weight=0.4)
    assert rate == 10.0  # 300/30, the 30-day rate alone
    assert diverged is False, "a missing window is not evidence of divergence"


def test_blended_daily_rate_DOES_blend_a_genuine_zero_sales_week():
    """The other half of the same distinction: a REAL zero-sales week (the window exists, it
    says 0) is data, and IS blended at the normal weight — collapsing 'no data' and 'zero
    data' into the same behaviour is the mutation this test exists to catch.
    """
    rate, diverged = logic.blended_daily_rate(kg_30d=300.0, kg_7d=0.0, weight=0.4)
    # 30d/day = 10.0, 7d/day = 0.0 -> blended = 0.4*0 + 0.6*10 = 6.0
    assert rate == 6.0
    assert diverged is True


def test_blended_daily_rate_handles_a_dead_parent():
    rate, diverged = logic.blended_daily_rate(kg_30d=0.0, kg_7d=0.0, weight=0.4)
    assert rate == 0.0
    assert diverged is False, "0 vs 0 is agreement, not divergence"


# ─── blend settings: range-checked on read and write ──────────────────────────


def test_default_blend_weight_and_threshold():
    assert logic.DEFAULT_BLEND == {"seven_day_weight": 0.4, "divergence_pct": 30.0}


def test_blend_setting_error_refuses_an_unknown_key():
    assert logic.blend_setting_error("bogus", 5) is not None


def test_blend_setting_error_refuses_an_out_of_range_weight():
    """The good_rating: 99 lesson — a weight of 99 would mean 'ignore the 30-day figure
    entirely and pretend last week is the only history that exists', silently."""
    assert logic.blend_setting_error("seven_day_weight", 99) is not None
    assert logic.blend_setting_error("seven_day_weight", 0.4) is None


def test_blend_setting_error_refuses_a_non_numeric_value():
    assert logic.blend_setting_error("seven_day_weight", "lots") is not None


def test_blend_or_default_merges_over_the_defaults():
    merged = logic.blend_or_default({"seven_day_weight": 0.5})
    assert merged == {"seven_day_weight": 0.5, "divergence_pct": 30.0}


def test_blend_or_default_discards_an_invalid_stored_value():
    """Validated on READ, not only on write — a value already in the database, or edited by
    hand, must not keep weakening the setting with nothing on screen explaining why."""
    merged = logic.blend_or_default({"seven_day_weight": 500})
    assert merged["seven_day_weight"] == logic.DEFAULT_BLEND["seven_day_weight"]
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly`
Expected: `AttributeError: module 'app.projections.logic' has no attribute 'sales_kg_by_parent'`
(and similarly for the other new names).

- [ ] **Step 3: Append the implementation to `app/projections/logic.py`**

```python
def sales_kg_by_parent(snapshot_rows: list[dict], groups: Mapping[str, dict]) -> dict[str, float]:
    """`units_ordered x pack weight`, summed per parent name. **Never `net_units`** — see the
    module docstring's cross-reference and the test fixture's own note: `net_units` goes negative
    on a refund-heavy week (measured: 2 ASINs in a real 7-day window), and a returns problem is
    not a demand signal.

    `snapshot_rows` is Amazon's nested row shape — the same shape
    `app.portfolio.economics.fetch_economics` returns fresh and
    `app.portfolio.repository.load_snapshot` reconstructs from storage, so this function does not
    care which one supplied it. A row for an ASIN outside `groups` (discontinued, or unknown to
    the sheet) contributes to no parent — it is not this function's job to invent one.
    """
    asin_to_parent: dict[str, tuple[str, float]] = {}
    for parent, group in groups.items():
        for asin in group["asins"]:
            asin_to_parent[asin] = (parent, group["weights"].get(asin) or 0)

    totals: dict[str, float] = {}
    for row in snapshot_rows:
        asin = (row.get("childAsin") or "").strip().upper()
        mapping = asin_to_parent.get(asin)
        if not mapping:
            continue
        parent, weight = mapping
        units = int((row.get("sales") or {}).get("unitsOrdered") or 0)
        totals[parent] = totals.get(parent, 0.0) + units * weight
    return {parent: round(kg, 2) for parent, kg in totals.items()}


def blended_daily_rate(
    kg_30d: float, kg_7d: float | None, weight: float, *, divergence_fraction: float = 0.30,
) -> tuple[float, bool]:
    """`(rate, diverged)` — the daily kg/day to forecast from, and whether the 7-day and 30-day
    windows disagreed enough to flag on screen.

    **`kg_7d=None` and `kg_7d=0.0` are different facts, and this is the whole point of the
    function.** `None` means no 7-day snapshot exists yet for this parent — the window is
    missing, not zero — and the honest answer is the 30-day rate alone. `0.0` means the window
    exists and genuinely recorded no sales that week, which IS real evidence and IS blended at
    the normal weight. Collapsing the two would cut a slow mover's forecast by the blend weight
    every time the 7-day fetch simply had not run yet, which is the common case on any given day.

    `divergence_fraction` is a PARAMETER, not a hardcoded constant, because it is a saved,
    editable setting (`DEFAULT_BLEND['divergence_pct'] / 100`) — the refresh job (Task 6) loads
    it from storage and passes the owner's own threshold. The default of 0.30 matches
    `DEFAULT_BLEND` exactly and is only what a caller gets for free if it never loads a setting.
    """
    rate_30 = (kg_30d or 0.0) / 30
    if kg_7d is None:
        return round(rate_30, 2), False

    rate_7 = kg_7d / 7
    blended = weight * rate_7 + (1 - weight) * rate_30
    if rate_30 == 0:
        diverged = rate_7 != 0
    else:
        diverged = abs(rate_7 / rate_30 - 1) > divergence_fraction
    return round(blended, 2), diverged


#: The blend weight and divergence threshold, editable and range-checked — the same pattern
#: `app.ads.logic.DEFAULT_GUARDRAILS` / `GUARDRAIL_RANGES` / `guardrail_error` establishes, mirrored
#: with its own names because both are hardcoded to their own `PortfolioSettings.name` row and
#: neither owns the concept of "a saved, range-checked JSON setting" generally.
DEFAULT_BLEND = {
    #: How much weight the last 7 days carries against the last 30. 0.4 is a starting point
    #: measured to move real parents meaningfully (Bangla Moori-shaped: 1.74x) without letting
    #: one freak week dominate a monthly purchasing decision.
    "seven_day_weight": 0.4,
    #: The |7d/30d - 1| fraction, as a PERCENTAGE for the settings screen, above which a row is
    #: flagged diverged. 30% — smaller than the real spikes measured (58-74%) so genuine signal
    #: is not missed, larger than ordinary week-to-week noise.
    "divergence_pct": 30.0,
}

#: Bounds for each blend setting. Same lesson as `app.ads.logic.GUARDRAIL_RANGES`: a
#: `good_rating: 99`-shaped mistake here (`seven_day_weight: 99`) would mean "ignore the 30-day
#: figure and treat one week as the whole history" — silently, with nothing to catch it.
BLEND_RANGES = {
    "seven_day_weight": (0.0, 1.0),
    "divergence_pct": (1.0, 200.0),
}


def blend_setting_error(key: str, value) -> str | None:
    """The REASON a blend setting is refused, or `None` if acceptable. Prose, not a bare False,
    so a refusal can say what the units are — the same shape as `app.ads.logic.guardrail_error`.
    """
    if key not in DEFAULT_BLEND:
        valid = ", ".join(sorted(DEFAULT_BLEND))
        return f"Unknown setting {key!r}. Valid names: {valid}."
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{key} must be a number, got {value!r}."
    if number != number or number in (float("inf"), float("-inf")):
        return f"{key} must be a number, got {value!r}."
    low, high = BLEND_RANGES[key]
    if not (low <= number <= high):
        if key == "seven_day_weight":
            return (f"{key} must be between {low:g} and {high:g} — it is a fraction of the "
                     f"blend, got {number:g}.")
        return f"{key} must be between {low:g} and {high:g}, got {number:g}."
    return None


def blend_or_default(stored: Mapping | None) -> dict:
    """Merge stored blend settings over the defaults, discarding any value that fails its range.

    **Validated on READ, not only on write** — a value already in the database, or edited by
    hand outside the app, must not keep silently distorting every parent's forecast.
    """
    merged = dict(DEFAULT_BLEND)
    for key, value in (stored or {}).items():
        if blend_setting_error(key, value) is None:
            merged[key] = float(value)
    return merged
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly`
Expected: all pass (13 + 17 = 30 tests).

- [ ] **Step 5: Commit**

```bash
git add app/projections/logic.py tests/test_projections_logic.py
git commit -m "feat(projections): 7d/30d sales blend and settings validation"
```

---

## Task 4: `app/projections/repository.py` — the only SQL for `ProjectionRow`/`ProjectionRefresh`

**Files:**
- Create: `app/projections/repository.py`
- Create: `tests/test_projections_repository.py`

**Interfaces:**
- Consumes: `ProjectionRow`, `ProjectionRefresh` from Task 1; nothing from `logic.py` — this module
  is pure SQL, matching `app/ads/repository.py`'s split.
- Produces (used by Task 5's router and Task 6's refresh job):
  - `async def load_rows(db) -> list[dict]`
  - `async def save_row(db, parent_product: str, values: dict, *, source: str, updated_by: str = "") -> dict`
  - `async def upsert_sheet_rows(db, rows: list[dict]) -> int` — bulk write from the weekly job,
    skips any row whose stored `sales_source == "manual"`
  - `async def reset_to_sheet(db, parent_product: str) -> dict | None`
  - `async def load_blend_settings(db) -> dict`
  - `async def save_blend_settings(db, values: dict, *, updated_by: str = "") -> dict` (raises
    `ValueError` on an invalid key/value, mirroring `app.ads.repository.save_guardrails`)
  - `async def reset_blend_settings(db) -> dict`
  - `async def record_refresh(db, *, window_start, window_end, rows_stored, error=None, started_at=None) -> None`
  - `async def last_refresh(db) -> dict | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_projections_repository.py`:

```python
"""The only SQL for the Projections tab. Every Decimal must come back as float — see
app.models.ProjectionRow's docstring and the two prior defects (orders payload datetimes,
raw_kg) this app has already shipped from forgetting that conversion.
"""
import pytest

from app.projections import logic, repository

pytestmark = pytest.mark.asyncio


async def test_save_row_then_load_returns_a_float_not_a_decimal(db):
    saved = await repository.save_row(
        db, "Chana Sattu", {"purchase_rate": 120.0, "daily_rate": 5.5}, source="sheet",
    )
    assert isinstance(saved["purchase_rate"], float)
    assert isinstance(saved["daily_rate"], float)

    rows = await repository.load_rows(db)
    assert rows[0]["purchase_rate"] == 120.0


async def test_save_row_upserts_by_parent_name(db):
    """A repeated save for the same parent updates the one row rather than doubling it — the
    same SELECT-then-UPDATE-or-INSERT idiom `save_raw_stock` uses."""
    await repository.save_row(db, "Chana Sattu", {"purchase_rate": 100.0}, source="sheet")
    await repository.save_row(db, "Chana Sattu", {"purchase_rate": 150.0}, source="sheet")

    rows = await repository.load_rows(db)
    assert len(rows) == 1
    assert rows[0]["purchase_rate"] == 150.0


async def test_a_manual_edit_marks_the_row_manual(db):
    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 999.0}, source="manual")
    rows = await repository.load_rows(db)
    assert rows[0]["sales_source"] == "manual"


async def test_upsert_sheet_rows_skips_a_manually_edited_row(db):
    """The rule the whole 'manual overrides survive a refresh' requirement rests on. A weekly
    recompute must not silently discard a hand-typed correction."""
    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 999.0}, source="manual")
    await repository.save_row(db, "Govind Bhog Rice", {"last_month_sale": 10.0}, source="sheet")

    updated = await repository.upsert_sheet_rows(db, [
        {"parent_product": "Chana Sattu", "last_month_sale": 5.0, "daily_rate": 1.0},
        {"parent_product": "Govind Bhog Rice", "last_month_sale": 20.0, "daily_rate": 2.0},
    ])

    rows = {r["parent_product"]: r for r in await repository.load_rows(db)}
    assert rows["Chana Sattu"]["last_month_sale"] == 999.0, "a manual row was overwritten by a refresh"
    assert rows["Govind Bhog Rice"]["last_month_sale"] == 20.0, "a sheet row was not updated"
    assert updated == 1, "the skipped manual row must not count as updated"


async def test_upsert_sheet_rows_creates_a_new_row_for_a_first_seen_parent(db):
    updated = await repository.upsert_sheet_rows(db, [
        {"parent_product": "Triphala Sattu", "last_month_sale": 3.0, "needs_review": True},
    ])
    assert updated == 1
    rows = await repository.load_rows(db)
    assert rows[0]["parent_product"] == "Triphala Sattu"
    assert rows[0]["needs_review"] is True


async def test_reset_to_sheet_clears_the_manual_flag(db):
    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 999.0}, source="manual")
    result = await repository.reset_to_sheet(db, "Chana Sattu")
    assert result["sales_source"] == "sheet"


async def test_reset_to_sheet_is_none_for_an_unknown_parent(db):
    assert await repository.reset_to_sheet(db, "Nonexistent") is None


# ─── blend settings ────────────────────────────────────────────────────────────


async def test_blend_settings_round_trip(db):
    saved = await repository.save_blend_settings(db, {"seven_day_weight": 0.5})
    assert saved["seven_day_weight"] == 0.5
    loaded = await repository.load_blend_settings(db)
    assert loaded["seven_day_weight"] == 0.5


async def test_blend_settings_reset_by_deleting_the_row(db):
    await repository.save_blend_settings(db, {"seven_day_weight": 0.9})
    reset = await repository.reset_blend_settings(db)
    assert reset == logic.DEFAULT_BLEND


async def test_save_blend_settings_raises_on_an_invalid_value(db):
    with pytest.raises(ValueError, match="seven_day_weight"):
        await repository.save_blend_settings(db, {"seven_day_weight": 99})


async def test_load_blend_settings_defaults_when_never_saved(db):
    assert await repository.load_blend_settings(db) == logic.DEFAULT_BLEND


# ─── refresh history ───────────────────────────────────────────────────────────


async def test_record_refresh_then_last_refresh_round_trips(db):
    await repository.record_refresh(
        db, window_start="2026-08-02", window_end="2026-08-31", rows_stored=47,
    )
    last = await repository.last_refresh(db)
    assert last["rows_stored"] == 47
    assert last["error"] == ""
    assert isinstance(last["started_at"], str), "a datetime reaching JSON must be pre-serialised"


async def test_last_refresh_is_none_when_never_run(db):
    assert await repository.last_refresh(db) is None


async def test_last_refresh_returns_the_newest_row(db):
    await repository.record_refresh(db, window_start="2026-07-01", window_end="2026-07-30", rows_stored=1)
    await repository.record_refresh(db, window_start="2026-08-02", window_end="2026-08-31", rows_stored=2)
    last = await repository.last_refresh(db)
    assert last["rows_stored"] == 2
```

- [ ] **Step 2: Run to verify the tests fail**

Run: `venv/Scripts/python -m pytest tests/test_projections_repository.py -q -p no:randomly`
Expected: `ModuleNotFoundError: No module named 'app.projections.repository'`.

- [ ] **Step 3: Write `app/projections/repository.py`**

```python
"""The only reader and writer of `projection_row` and `projection_refresh`.

SELECT-then-UPDATE-or-INSERT throughout, the same dialect-neutral idiom
`app.orders.repository.save_raw_stock` and `app.shipment.repository` document — one code path
runs identically on SQLite locally and PostgreSQL in production.

**Every Decimal is cast to float on the way out.** SQLAlchemy returns `Decimal` for `Numeric`
columns and `JSONResponse` cannot serialise it. This app has already shipped that exact defect
twice (orders payload datetimes, then `raw_kg`), both found in a browser on production — done
once here so every route inherits the fix.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSettings, ProjectionRefresh, ProjectionRow
from app.projections import logic

logger = logging.getLogger(__name__)

#: `PortfolioSettings.name` for the blend weight/threshold. That table is shared and name-keyed
#: across features — Portfolio's own verdict thresholds live under `"thresholds"`, and the Ads
#: guardrails live under `GUARDRAIL_SETTING_NAME` — so this follows the same pattern under its
#: own name rather than reusing either feature's specific load/save functions.
BLEND_SETTING_NAME = "projection_blend"

#: Every column on `ProjectionRow` a caller may read or write, EXCLUDING the primary key and the
#: audit columns (`updated_at`, `updated_by`) — kept as one tuple so `_row_to_dict` and
#: `save_row`'s `setattr` loop cannot drift about which fields exist.
_FIELDS = (
    "parent_product", "brand", "purchase_rate", "supplier_to_wh", "packing", "wh_to_ixd",
    "ixd_to_fba", "wh_buffer_days", "seasonal_impact", "growth_rate", "needs_review",
    "sales_source", "last_month_sale", "seven_day_rate", "thirty_day_rate", "daily_rate",
    "diverged", "current_fba_stock", "current_wh_stock",
)

#: Which of the fields above are Decimal-backed and need the float conversion on the way out.
_NUMERIC_FIELDS = (
    "purchase_rate", "wh_buffer_days", "seasonal_impact", "growth_rate", "last_month_sale",
    "seven_day_rate", "thirty_day_rate", "daily_rate", "current_fba_stock", "current_wh_stock",
)


def _row_to_dict(row: ProjectionRow) -> dict:
    out = {}
    for field in _FIELDS:
        value = getattr(row, field)
        out[field] = float(value) if field in _NUMERIC_FIELDS and value is not None else value
    return out


async def load_rows(db: AsyncSession) -> list[dict]:
    """Every stored parent row, as plain dicts with floats — never `Decimal`."""
    rows = (await db.execute(select(ProjectionRow))).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def save_row(
    db: AsyncSession, parent_product: str, values: dict, *, source: str, updated_by: str = "",
) -> dict:
    """Upsert one parent's row. `source` is stamped on EVERY call — there is no field left
    unset, so a caller cannot accidentally leave a row's provenance stale.
    """
    existing = (
        await db.execute(select(ProjectionRow).where(ProjectionRow.parent_product == parent_product))
    ).scalar_one_or_none()

    if existing is None:
        existing = ProjectionRow(parent_product=parent_product)
        db.add(existing)

    for key, value in values.items():
        if key in _FIELDS and key != "parent_product":
            setattr(existing, key, value)
    existing.sales_source = source
    existing.updated_at = datetime.utcnow()
    existing.updated_by = updated_by or existing.updated_by

    await db.commit()
    await db.refresh(existing)
    return _row_to_dict(existing)


async def upsert_sheet_rows(db: AsyncSession, rows: list[dict]) -> int:
    """Bulk-write computed rows from the weekly refresh. Returns how many rows were actually
    updated — **a row whose stored `sales_source == "manual"` is SKIPPED and not counted**, which
    is the entire mechanism behind "a manual override survives a refresh". A brand-new parent
    (no existing row at all) is created with `sales_source="sheet"`.
    """
    if not rows:
        return 0

    names = [r["parent_product"] for r in rows]
    existing = {
        row.parent_product: row
        for row in (
            await db.execute(select(ProjectionRow).where(ProjectionRow.parent_product.in_(names)))
        ).scalars()
    }

    written = 0
    now = datetime.utcnow()
    for incoming in rows:
        name = incoming["parent_product"]
        current = existing.get(name)
        if current is not None and current.sales_source == "manual":
            continue
        if current is None:
            current = ProjectionRow(parent_product=name)
            db.add(current)
        for key, value in incoming.items():
            if key in _FIELDS and key != "parent_product":
                setattr(current, key, value)
        current.sales_source = "sheet"
        current.updated_at = now
        written += 1

    await db.commit()
    return written


async def reset_to_sheet(db: AsyncSession, parent_product: str) -> dict | None:
    """Clear a manual override, so the next scheduled recompute (or a manual "Refresh now")
    updates it again. `None` if no row exists for that name — nothing to reset.
    """
    row = (
        await db.execute(select(ProjectionRow).where(ProjectionRow.parent_product == parent_product))
    ).scalar_one_or_none()
    if row is None:
        return None
    row.sales_source = "sheet"
    await db.commit()
    await db.refresh(row)
    return _row_to_dict(row)


# ─── Blend settings ────────────────────────────────────────────────────────────


async def load_blend_settings(db: AsyncSession) -> dict:
    """The saved blend weight/threshold, merged over the measured defaults. Range-checked on
    the way out — see `app.projections.logic.blend_or_default`."""
    row = (
        await db.execute(select(PortfolioSettings).where(PortfolioSettings.name == BLEND_SETTING_NAME))
    ).scalar_one_or_none()
    stored = {}
    if row and row.value_json:
        try:
            stored = json.loads(row.value_json) or {}
        except json.JSONDecodeError:
            logger.warning("projections: stored blend settings are not valid JSON; using defaults")
    return logic.blend_or_default(stored)


async def save_blend_settings(db: AsyncSession, values: dict, *, updated_by: str = "") -> dict:
    """Validate and store the blend settings. Raises `ValueError` naming the first problem —
    the same shape as `app.ads.repository.save_guardrails`."""
    for key, value in (values or {}).items():
        problem = logic.blend_setting_error(key, value)
        if problem:
            raise ValueError(problem)

    merged = logic.blend_or_default(values)
    row = (
        await db.execute(select(PortfolioSettings).where(PortfolioSettings.name == BLEND_SETTING_NAME))
    ).scalar_one_or_none()
    if row:
        row.value_json = json.dumps(merged)
        row.updated_by = updated_by or row.updated_by
    else:
        db.add(PortfolioSettings(
            name=BLEND_SETTING_NAME, value_json=json.dumps(merged), updated_by=updated_by,
        ))
    await db.commit()
    return merged


async def reset_blend_settings(db: AsyncSession) -> dict:
    """Delete the stored row so the measured defaults apply again."""
    row = (
        await db.execute(select(PortfolioSettings).where(PortfolioSettings.name == BLEND_SETTING_NAME))
    ).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return dict(logic.DEFAULT_BLEND)


# ─── Refresh history ───────────────────────────────────────────────────────────


async def record_refresh(
    db: AsyncSession, *, window_start: str | None, window_end: str | None, rows_stored: int,
    error: str | None = None, started_at: datetime | None = None,
) -> None:
    """Log one refresh attempt, successful or not — the same shape as
    `app.portfolio.repository.record_refresh`."""
    db.add(ProjectionRefresh(
        window_start=window_start, window_end=window_end, rows_stored=rows_stored, error=error,
        started_at=started_at or datetime.utcnow(), finished_at=datetime.utcnow(),
    ))
    await db.commit()


async def last_refresh(db: AsyncSession) -> dict | None:
    """The newest refresh attempt, JSON-safe, or `None` if it has never run."""
    row = (
        await db.execute(
            select(ProjectionRefresh)
            .order_by(ProjectionRefresh.started_at.desc(), ProjectionRefresh.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "window_start": row.window_start,
        "window_end": row.window_end,
        "rows_stored": int(row.rows_stored or 0),
        "error": row.error or "",
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_repository.py -q -p no:randomly`
Expected: all pass (14 tests — verified by count during execution; plan's original estimate of
15 was off by one).

- [ ] **Step 5: Commit**

```bash
git add app/projections/repository.py tests/test_projections_repository.py
git commit -m "feat(projections): repository for projection_row and projection_refresh"
```

---

## Task 5: rewrite `app/routers/projections.py` around the live sheet and the DB

**Files:**
- Modify: `app/routers/projections.py` (the whole file — see the exact replacement below)
- Modify: `app/projections/logic.py` (append one function, `hidden_parent_names`)
- Modify: `tests/test_projections_logic.py` (append)
- Create: `tests/test_projections_api.py`

**Interfaces:**
- Consumes: `catalogue.load_catalogue()` (existing, `app/shipment/catalogue.py`);
  `logic.group_active_by_name`, `build_parent_config`, `calculate_projections` (Task 2/3);
  `repository.load_rows`, `save_row`, `upsert_sheet_rows`, `reset_to_sheet`,
  `load_blend_settings`, `save_blend_settings`, `reset_blend_settings`, `last_refresh` (Task 4).
- Produces: `GET /projections/last` returns
  `{products, catalogue, blend, last_refresh}` (`catalogue` is the `{source, active_parents,
  hidden_count, hidden_names, warning}` report); every other route listed in Step 3.

**Important test-suite fact to know before writing tests:** `tests/conftest.py` has an
**autouse** fixture, `no_live_product_sheet`, that patches
`app.shipment.catalogue.load_catalogue` to return an EMPTY catalogue (`{}, None, "none"`) for
**every** test unless the test supplies its own patch. Every test in `tests/test_projections_api.py`
that needs active parents to exist MUST monkeypatch `app.shipment.catalogue.load_catalogue`
itself, the same way `tests/test_product_pricing.py`'s `fake_catalogue` fixture does (see Step 5).

- [ ] **Step 1: Add `hidden_parent_names` to `app/projections/logic.py`, with its failing test first**

Append to `tests/test_projections_logic.py`:

```python
# ─── hidden_parent_names ───────────────────────────────────────────────────────


def test_hidden_parent_names_are_stored_parents_absent_from_the_live_groups():
    stored_names = {"Chana Sattu", "Kasundi", "Bengali Posta"}
    live_groups = {"Chana Sattu": {}}
    hidden = logic.hidden_parent_names(stored_names, live_groups)
    assert hidden == ["Bengali Posta", "Kasundi"], "not sorted, or not exactly the missing set"


def test_hidden_parent_names_is_empty_when_everything_stored_is_still_active():
    assert logic.hidden_parent_names({"Chana Sattu"}, {"Chana Sattu": {}}) == []
```

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly -k hidden`
Expected: `AttributeError` — the function does not exist yet.

Append to `app/projections/logic.py`:

```python
def hidden_parent_names(stored_names: set[str], live_groups: Mapping[str, dict]) -> list[str]:
    """Which stored parents are no longer active in the sheet, sorted, for the screen's
    hidden-parents note. **Named, never a bare count** — a parent silently missing from a
    91-row list is indistinguishable from a bug, the same rule the Shipment tab's catalogue
    diff follows.
    """
    return sorted(stored_names - set(live_groups))
```

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly -k hidden`
Expected: both pass.

- [ ] **Step 2: Commit the logic addition on its own**

```bash
git add app/projections/logic.py tests/test_projections_logic.py
git commit -m "feat(projections): name the parents hidden by the live catalogue"
```

- [ ] **Step 3: Replace `app/routers/projections.py` entirely**

The whole file becomes:

```python
"""Projections — forecast next month's sales from live data, and reorder alerts.

**Parents come from the MRP sheet's own active-and-name grouping, never from
`app/invoice/product_families.json`.** That static file is what left Triphala Sattu invisible
here in the first place — active in the sheet, in two pack sizes, never added to the file, and
the file was the source of every row this screen showed. `app.shipment.catalogue.load_catalogue()`
is the same source and fallback chain (sheet -> cached copy -> static file) the Shipment tab
already relies on for exactly this reason.

**Sales come from `economics_snapshot`, already stored by the Portfolio tab's nightly refresh —
no new Amazon integration.** The manual Business Report CSV upload stays as an explicit override:
any edit through `/calculate` or `/upload-csv` marks that parent's row `sales_source="manual"`,
and a manual row is skipped by the weekly recompute (`app.projections.refresh.run`) until the
owner explicitly resets it through `/reset-row`.
"""
import io
import json
import logging
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Request, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.projections import logic, repository
from app.routers.auth import require_auth
from app.shipment import catalogue

router = APIRouter(prefix="/projections")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DEFAULTS_FILE = BASE_DIR / "invoice" / "projection_defaults.json"
FAMILIES_FILE = BASE_DIR / "invoice" / "product_families.json"


def load_defaults() -> dict:
    if DEFAULTS_FILE.exists():
        with open(DEFAULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


DEFAULTS = load_defaults()

#: The config a parent gets when nothing in `projection_defaults.json` matches its name. Global
#: Defaults, editable on screen, unchanged from the pre-existing behaviour for an unmatched row.
GLOBAL_DEFAULTS = {
    "growth_rate": 0.3, "seasonal_impact": 1.0, "supplier_to_wh": 5, "packing": 2,
    "wh_to_ixd": 10, "ixd_to_fba": 5, "wh_buffer_days": 10.0,
}


async def build_current_rows(db: AsyncSession) -> tuple[list[dict], dict]:
    """Every currently-active parent, merged with its stored row (sales, any manual edit) or a
    freshly-built one. Returns `(rows, catalogue_report)`.

    **A brand-new parent (no stored row yet) is written to the database here**, with
    `sales_source="sheet"` and zero sales, so it exists for the weekly refresh to update and is
    never re-synthesised on every page load. A parent hidden this load (no longer active) is left
    in the database untouched — its row is not deleted, only excluded from what is returned, so a
    reactivated product keeps its history rather than starting over.
    """
    sheet_products, sheet_warning, sheet_source = await catalogue.load_catalogue()
    live_groups = logic.group_active_by_name(sheet_products)

    stored = {r["parent_product"]: r for r in await repository.load_rows(db)}
    hidden = logic.hidden_parent_names(set(stored), live_groups)

    rows: list[dict] = []
    for name, group in live_groups.items():
        if name in stored:
            rows.append(stored[name])
            continue
        config = logic.build_parent_config(name, group, DEFAULTS, GLOBAL_DEFAULTS)
        created = await repository.save_row(
            db, name,
            {**config, "last_month_sale": 0, "seven_day_rate": None, "thirty_day_rate": None,
             "daily_rate": 0, "diverged": False, "current_fba_stock": 0, "current_wh_stock": 0},
            source="sheet",
        )
        rows.append(created)

    report = {
        "source": sheet_source,
        "active_parents": len(live_groups),
        "hidden_count": len(hidden),
        "hidden_names": hidden[:8],
        "warning": sheet_warning,
    }
    return rows, report


@router.get("/last")
async def get_current(request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """The live table: every active parent, its purchasing config, and its sales rate.

    Replaces the old file-backed `/last`+`/init` pair. There is no "never initialised" state any
    more — the sheet always has active parents, so this always has rows to show.
    """
    rows, report = await build_current_rows(db)
    return JSONResponse({
        "products": logic.calculate_projections(rows),
        "catalogue": report,
        "blend": await repository.load_blend_settings(db),
        "last_refresh": await repository.last_refresh(db),
    })


@router.post("/calculate")
async def calculate(
    request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db),
):
    """Persist the owner's edited values for every product in the body, recompute, and return.

    **Every product in the body is saved as `sales_source="manual"`** — the table has no way to
    tell "the owner retyped this number" from "this is what the sheet already said", so treating
    every save through this route as an edit is the safe direction. A parent whose numbers
    genuinely came from the sheet keeps reading that way until the next weekly refresh recomputes
    it; this route is only reached when the owner pressed Recalculate.
    """
    body = await request.json()
    products = body.get("products", [])

    saved = []
    for p in products:
        name = p.get("product") or p.get("parent_product")
        if not name:
            continue
        row = await repository.save_row(db, name, {
            "purchase_rate": p.get("purchase_rate", 0), "supplier_to_wh": p.get("supplier_to_wh", 5),
            "packing": p.get("packing", 2), "wh_to_ixd": p.get("wh_to_ixd", 10),
            "ixd_to_fba": p.get("ixd_to_fba", 5), "wh_buffer_days": p.get("wh_buffer_days", 10),
            "seasonal_impact": p.get("seasonal_impact", 1.0), "growth_rate": p.get("growth_rate", 0.3),
            "last_month_sale": p.get("last_month_sale", 0),
            "current_fba_stock": p.get("current_fba_stock", 0),
            "current_wh_stock": p.get("current_wh_stock", 0),
        }, source="manual")
        saved.append(row)

    products = logic.calculate_projections(saved)
    total_forecast = sum(p["monthly_forecast"] for p in products)
    total_ideal_value = sum(p["ideal_stock_value"] for p in products)
    total_current_value = sum(p["current_stock_value"] for p in products)
    ship_alerts = sum(1 for p in products if p["shipment_alert"] > 0)
    reorder_alerts = sum(1 for p in products if p["reorder_alert"] > 0)
    critical_alerts = sum(1 for p in products if p.get("inventory_days", 999) < 7)

    return JSONResponse({
        "products": products,
        "summary": {
            "total_products": len(products),
            "total_forecast_kg": round(total_forecast, 0),
            "total_ideal_value": round(total_ideal_value, 0),
            "total_current_value": round(total_current_value, 0),
            "shipment_alerts": ship_alerts,
            "reorder_alerts": reorder_alerts,
            "critical_alerts": critical_alerts,
        },
    })


@router.post("/reset-row")
async def reset_row(request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Clear one parent's manual override, body: `{"parent_product": "..."}`. The next weekly
    refresh (or a manual one) will then update it again."""
    body = await request.json()
    name = (body.get("parent_product") or "").strip()
    if not name:
        return JSONResponse({"error": "parent_product is required."}, status_code=400)
    result = await repository.reset_to_sheet(db, name)
    if result is None:
        return JSONResponse({"error": f"No row found for {name!r}."}, status_code=404)
    return JSONResponse({"row": result})


@router.get("/blend-settings")
async def get_blend_settings(_=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """The blend weight and divergence threshold, with their bounds — for the settings panel."""
    return {
        "blend": await repository.load_blend_settings(db),
        "defaults": dict(logic.DEFAULT_BLEND),
        "ranges": {k: list(v) for k, v in logic.BLEND_RANGES.items()},
        "help": {
            "seven_day_weight": "How much the last 7 days counts against the last 30 when "
                                 "forecasting. Higher reacts faster to a real spike or drop; "
                                 "lower is steadier against a noisy week.",
            "divergence_pct": "When the 7-day and 30-day rates disagree by more than this, the "
                               "row is flagged so you can see why its forecast moved.",
        },
    }


@router.post("/blend-settings")
async def set_blend_settings(
    request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db),
):
    """Edit the blend weight/threshold, or reset to the measured defaults."""
    body = await request.json()
    if body.get("reset"):
        return {"blend": await repository.reset_blend_settings(db), "status": "reset"}
    try:
        saved = await repository.save_blend_settings(db, body.get("blend") or {})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"blend": saved, "status": "saved"}


@router.post("/refresh-now")
async def refresh_now(_=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Run the weekly 7d/30d recompute immediately — "I want it now", the same button every
    other refreshable tab in this app offers."""
    from app.projections import refresh

    result = await refresh.run(db)
    return JSONResponse(result)


@router.delete("/clear")
async def clear_all_overrides(_=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Reset EVERY parent's manual override at once. The pre-existing "Clear" button's meaning
    changes with this feature: there is no longer a file to delete, and the equivalent action —
    discarding every hand-typed correction so the next refresh recomputes everything from the
    sheet — is this.
    """
    rows = await repository.load_rows(db)
    for row in rows:
        if row["sales_source"] == "manual":
            await repository.reset_to_sheet(db, row["parent_product"])
    return JSONResponse({"status": "cleared", "reset_count": sum(
        1 for r in rows if r["sales_source"] == "manual"
    )})


# ─── CSV upload: an explicit manual override, not a live source ────────────────

def _clean_number(val) -> float:
    if val is None or str(val).strip() in ("", "-", "nan"):
        return 0.0
    import re
    cleaned = re.sub(r"[₹,%\s]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_business_report_for_projections(content: bytes, groups: dict[str, dict]) -> dict[str, float]:
    """Business Report CSV -> `{parent_name: total_kg_sold}`, using the LIVE sheet's own
    ASIN->parent grouping (`groups`, from `logic.group_active_by_name`) — never
    `product_families.json`. An ASIN outside every active group (discontinued, or unknown to the
    sheet) is skipped, the same rule `sales_kg_by_parent` follows for economics rows.
    """
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception:
        df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")

    child_col = "(Child) ASIN"
    if child_col not in df.columns:
        raise ValueError("Could not find '(Child) ASIN' column. Upload Amazon Business Report (By ASIN).")

    asin_to_parent: dict[str, tuple[str, float]] = {}
    for parent, group in groups.items():
        for asin in group["asins"]:
            asin_to_parent[asin] = (parent, group["weights"].get(asin) or 0)

    product_kg: dict[str, float] = {}
    for _, row in df.iterrows():
        asin = str(row.get(child_col, "")).strip()
        if not asin or len(asin) < 10:
            continue
        mapping = asin_to_parent.get(asin.upper())
        if not mapping:
            continue
        parent, weight = mapping
        units = _clean_number(row.get("Units Ordered", 0))
        product_kg[parent] = product_kg.get(parent, 0) + units * weight

    return product_kg


@router.post("/upload-csv")
async def upload_csv(
    request: Request, file: UploadFile = File(...), _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Upload a Business Report CSV -> fill `last_month_sale` (kg) per active parent, marked
    as a manual override — see the module docstring."""
    content = await file.read()

    sheet_products, _warning, _source = await catalogue.load_catalogue()
    groups = logic.group_active_by_name(sheet_products)

    try:
        product_kg = parse_business_report_for_projections(content, groups)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    rows, _report = await build_current_rows(db)
    saved = []
    for row in rows:
        name = row["parent_product"]
        kg = round(product_kg.get(name, 0), 2)
        updated = await repository.save_row(db, name, {**row, "last_month_sale": kg}, source="manual")
        saved.append(updated)

    products = logic.calculate_projections(saved)
    products.sort(key=lambda x: x.get("monthly_forecast", 0), reverse=True)
    filled = sum(1 for p in products if p["last_month_sale"] > 0)

    return JSONResponse({
        "products": products,
        "total_products": len(products),
        "filled_from_csv": filled,
        "total_kg_from_csv": round(sum(product_kg.values()), 1),
        "total_forecast_kg": round(sum(p["monthly_forecast"] for p in products), 0),
    })


@router.get("/defaults")
async def get_defaults(request: Request, _=Depends(require_auth)):
    """Every entry in `projection_defaults.json`, unchanged from before this feature — kept for
    reference; not read by the current template."""
    return JSONResponse(DEFAULTS)


@router.get("/download")
async def download_projection(request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Download the current table as Excel."""
    rows, _report = await build_current_rows(db)
    products = logic.calculate_projections(rows)
    if not products:
        return JSONResponse({"error": "No projection data"}, status_code=404)

    out_rows = [{
        "Product": p["parent_product"],
        "Brand": p.get("brand", ""),
        "Last Month Sale (kg)": p.get("last_month_sale", 0),
        "Seasonal Impact": p.get("seasonal_impact", 1),
        "Growth Rate": p.get("growth_rate", 0.3),
        "Monthly Forecast (kg)": p.get("monthly_forecast", 0),
        "Daily Rate (kg)": p.get("daily_rate", 0),
        "Lead Time (days)": p.get("total_lead_time", 0),
        "Ideal FBA Stock (kg)": p.get("ideal_fba_stock", 0),
        "Current FBA Stock (kg)": p.get("current_fba_stock", 0),
        "Shipment Alert (kg)": p.get("shipment_alert", 0),
        "WH Buffer Days": p.get("wh_buffer_days", 0),
        "Ideal WH Stock (kg)": p.get("ideal_wh_stock", 0),
        "Current WH Stock (kg)": p.get("current_wh_stock", 0),
        "Reorder Alert (kg)": p.get("reorder_alert", 0),
        "Purchase Rate (Rs/kg)": p.get("purchase_rate", 0),
        "Ideal Stock Value (Rs)": p.get("ideal_stock_value", 0),
        "Inventory Days": p.get("inventory_days", 0),
        "Sales Source": p.get("sales_source", "sheet"),
        "Needs Review": "yes" if p.get("needs_review") else "",
    } for p in products]

    df = pd.DataFrame(out_rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Projections")
        ws = writer.sheets["Projections"]
        from openpyxl.styles import PatternFill
        red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
        yellow_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        for row_idx, p in enumerate(products, start=2):
            if p.get("shipment_alert", 0) > 0:
                ws.cell(row=row_idx, column=11).fill = red_fill
            if p.get("reorder_alert", 0) > 0:
                ws.cell(row=row_idx, column=15).fill = red_fill
            if p.get("inventory_days", 999) < 7:
                ws.cell(row=row_idx, column=18).fill = red_fill
            elif p.get("inventory_days", 999) < 14:
                ws.cell(row=row_idx, column=18).fill = yellow_fill

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Projections.xlsx"},
    )
```

**Note the removed routes**: `POST /projections/init` (no longer meaningful — there is no
separate "load defaults" step; `/last` always reflects the live sheet) and the old file-backed
`DELETE /projections/clear` behaviour (repurposed above to "reset every manual override", not
"delete all data" — there is no longer a file to delete). Task 7 removes the corresponding calls
from the template.

- [ ] **Step 4: Confirm the file imports cleanly**

Run: `venv/Scripts/python -c "import app.routers.projections"`
Expected: no error.

- [ ] **Step 5: Write `tests/test_projections_api.py`**

```python
"""API tests for the live-data Projections tab.

**Every test needing active parents must patch `app.shipment.catalogue.load_catalogue` itself.**
`tests/conftest.py`'s autouse `no_live_product_sheet` fixture returns an EMPTY catalogue for every
test by default — correct for the Shipment tests, wrong here, where the whole feature is "show
what the sheet says is active". The pattern below is `tests/test_product_pricing.py`'s
`fake_catalogue` fixture, adapted.
"""
import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_catalogue(monkeypatch):
    """Two active parents (one multi-size), one inactive, one Triphala-shaped (active, absent
    from projection_defaults.json under any spelling)."""
    async def _catalogue():
        return (
            {
                "B0CHANA001": {"asin": "B0CHANA001", "name": "Chana Sattu", "weight": 0.5,
                               "brand": "Mithila Foods", "active": True},
                "B0CHANA002": {"asin": "B0CHANA002", "name": "Chana Sattu", "weight": 1.0,
                               "brand": "Mithila Foods", "active": True},
                "B0GOVIND01": {"asin": "B0GOVIND01", "name": "Govind Bhog Rice", "weight": 1.0,
                               "brand": "Mithila Foods", "active": True},
                "B0DEAD0001": {"asin": "B0DEAD0001", "name": "Kasundi", "weight": 0.3,
                               "brand": "Howrah Foods", "active": False},
                "B0TRIPHAL1": {"asin": "B0TRIPHAL1", "name": "Triphala Sattu", "weight": 0.5,
                               "brand": "Mithila Foods", "active": True},
            },
            None,
            "sheet",
        )

    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _catalogue, raising=True)
    return _catalogue


async def test_last_returns_only_active_parents_by_name(auth_client, db, fake_catalogue):
    body = (await auth_client.get("/projections/last")).json()
    names = {p["parent_product"] for p in body["products"]}
    assert names == {"Chana Sattu", "Govind Bhog Rice", "Triphala Sattu"}, (
        "either an inactive parent leaked in, or an active one was hidden"
    )


async def test_triphala_sattu_appears_and_is_flagged_needs_review(auth_client, db, fake_catalogue):
    """The specific product that exposed why this had to be a source change, not a filter:
    active in the sheet, absent from projection_defaults.json under any spelling."""
    body = (await auth_client.get("/projections/last")).json()
    triphala = next(p for p in body["products"] if p["parent_product"] == "Triphala Sattu")
    assert triphala["needs_review"] is True
    assert triphala["purchase_rate"] == 0  # Global Defaults' purchase_rate, unset


async def test_a_matched_parent_is_not_flagged_needs_review(auth_client, db, fake_catalogue):
    body = (await auth_client.get("/projections/last")).json()
    govind = next(p for p in body["products"] if p["parent_product"] == "Govind Bhog Rice")
    assert govind["needs_review"] is False
    # Verified against the real file: json.load(open("app/invoice/projection_defaults.json"))
    # ["Govind Bhog Rice"]["purchase_rate"] == 150.0. If this fails after an unrelated edit to
    # that file, re-check the real value rather than "fixing" this assertion blindly.
    assert govind["purchase_rate"] == pytest.approx(150.0)  # from projection_defaults.json


async def test_the_hidden_parent_is_named_not_just_counted(auth_client, db, fake_catalogue):
    """Kasundi is inactive; it must not appear in products AND must be named in the report."""
    # First load with Kasundi active, to get it stored...
    body = (await auth_client.get("/projections/last")).json()
    assert "Kasundi" not in {p["parent_product"] for p in body["products"]}
    # Kasundi was never active in this fixture's catalogue at all, so it never enters storage
    # and cannot be "hidden" this call. Prove the OTHER direction instead: an existing stored
    # row for a name the current catalogue does not mention shows up as hidden.
    from app.projections import repository
    await repository.save_row(db, "Old Discontinued Product", {}, source="sheet")

    body = (await auth_client.get("/projections/last")).json()
    assert "Old Discontinued Product" in body["catalogue"]["hidden_names"]
    assert body["catalogue"]["hidden_count"] >= 1


async def test_calculate_marks_every_saved_row_manual(auth_client, db, fake_catalogue):
    await auth_client.get("/projections/last")  # seed the rows
    response = await auth_client.post("/projections/calculate", json={
        "products": [{"product": "Chana Sattu", "last_month_sale": 42.0}],
    })
    body = response.json()
    row = next(p for p in body["products"] if p["parent_product"] == "Chana Sattu")
    assert row["sales_source"] == "manual"
    assert row["last_month_sale"] == 42.0


async def test_a_manual_row_survives_the_next_last_call(auth_client, db, fake_catalogue):
    await auth_client.get("/projections/last")
    await auth_client.post("/projections/calculate", json={
        "products": [{"product": "Chana Sattu", "last_month_sale": 42.0}],
    })
    body = (await auth_client.get("/projections/last")).json()
    row = next(p for p in body["products"] if p["parent_product"] == "Chana Sattu")
    assert row["last_month_sale"] == 42.0, "the manual edit did not survive a page reload"


async def test_reset_row_clears_the_manual_flag(auth_client, db, fake_catalogue):
    await auth_client.get("/projections/last")
    await auth_client.post("/projections/calculate", json={
        "products": [{"product": "Chana Sattu", "last_month_sale": 42.0}],
    })
    response = await auth_client.post("/projections/reset-row", json={"parent_product": "Chana Sattu"})
    assert response.json()["row"]["sales_source"] == "sheet"


async def test_reset_row_refuses_an_unknown_parent(auth_client, db, fake_catalogue):
    response = await auth_client.post("/projections/reset-row", json={"parent_product": "Nope"})
    assert response.status_code == 404


# ─── blend settings ────────────────────────────────────────────────────────────


async def test_blend_settings_round_trip_through_the_api(auth_client, db):
    from app.projections import logic

    body = (await auth_client.get("/projections/blend-settings")).json()
    assert body["blend"]["seven_day_weight"] == logic.DEFAULT_BLEND["seven_day_weight"]

    saved = await auth_client.post("/projections/blend-settings", json={"blend": {"seven_day_weight": 0.6}})
    assert saved.json()["blend"]["seven_day_weight"] == 0.6

    reset = await auth_client.post("/projections/blend-settings", json={"reset": True})
    assert reset.json()["blend"]["seven_day_weight"] == logic.DEFAULT_BLEND["seven_day_weight"]


async def test_an_absurd_blend_weight_is_refused_with_its_reason(auth_client, db):
    response = await auth_client.post(
        "/projections/blend-settings", json={"blend": {"seven_day_weight": 99}},
    )
    assert response.status_code == 400
    assert "seven_day_weight" in response.json()["error"]


# ─── CSV upload marks manual ────────────────────────────────────────────────────


async def test_csv_upload_marks_the_row_manual(auth_client, db, fake_catalogue):
    csv_bytes = (
        "(Child) ASIN,Units Ordered\nB0CHANA001,20\n"
    ).encode("utf-8")
    files = {"file": ("report.csv", csv_bytes, "text/csv")}
    response = await auth_client.post("/projections/upload-csv", files=files)
    body = response.json()
    row = next(p for p in body["products"] if p["parent_product"] == "Chana Sattu")
    assert row["sales_source"] == "manual"
    assert row["last_month_sale"] == pytest.approx(10.0)  # 20 units * 0.5 kg
```

- [ ] **Step 6: Run to verify current failures make sense, then re-run once fixed**

Run: `venv/Scripts/python -m pytest tests/test_projections_api.py -q -p no:randomly`
Expected on the FIRST run: likely failures from small mismatches (route naming, response shape).
Read each failure and fix `app/routers/projections.py` to match — do not change the tests to
match a shortcut in the router; the tests encode the spec's decisions.

Re-run until: all pass (11 tests).

- [ ] **Step 7: Run the full existing suite to catch anything the router rewrite broke**

Run: `venv/Scripts/python -m pytest -q -p no:randomly`
Expected: same pass count as before this task plus the new tests, zero new failures. If anything
outside `tests/test_projections_*.py` fails, it is almost certainly a test that imported
`app.routers.projections.DATA_FILE`/`DEFAULTS_FILE`/`FAMILIES_FILE`/`calculate_projections`
directly — grep for those names across `tests/` and update the import path to
`app.projections.logic.calculate_projections` (there were none as of this plan's writing,
confirmed by `grep -rln "product_families\|DEFAULTS\b" app/ templates/` returning no hits outside
`app/routers/projections.py` itself — but re-check, since Task 5 changed that file).

- [ ] **Step 8: Commit**

```bash
git add app/routers/projections.py tests/test_projections_api.py
git commit -m "feat(projections): rebuild the router on the live sheet and the database"
```

---

## Task 6: `app/projections/refresh.py` — the weekly recompute job

**Files:**
- Create: `app/projections/refresh.py`
- Create: `tests/test_projections_refresh.py`
- Modify: `app/scheduler.py`
- Modify: `tests/test_retention_and_scheduler.py`

**Interfaces:**
- Consumes: `app.portfolio.economics.fetch_economics(days=N, sleep=...)` (existing, returns
  `(asin_rows, sku_rows, start, end)`); `app.portfolio.repository.save_snapshot`,
  `windows_available` (existing); `app.shipment.catalogue.load_catalogue` (existing);
  `app.projections.logic.group_active_by_name`, `sales_kg_by_parent`, `blended_daily_rate`
  (Tasks 2/3); `app.projections.repository.upsert_sheet_rows`, `load_blend_settings`,
  `record_refresh` (Task 4); `app.ist.utc_hhmm`, `ist.label` (existing, `app/ist.py`).
- Produces: `async def run(db: AsyncSession, *, sleep=asyncio.sleep, today: date | None = None) -> dict`
  returning `{"rows_stored": int, "error": str | None, "window_start": str, "window_end": str}` —
  the same result shape `scheduled_ads_refresh`/`scheduled_portfolio_refresh` read in
  `app/scheduler.py`. `today` defaults to `app.ist.today()`; a test may pin it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_projections_refresh.py`:

```python
"""The weekly 7d/30d recompute. Reuses app.portfolio.economics — no new Amazon integration."""
import pytest

from app.projections import refresh, repository

pytestmark = pytest.mark.asyncio


async def _no_sleep(_seconds):
    return None


def _fake_catalogue_fn(groups_source):
    async def _fn():
        return groups_source, None, "sheet"
    return _fn


async def test_run_stores_a_blended_rate_from_two_fetched_windows(db, monkeypatch):
    """The whole point measured against the real account: 30d and 7d windows both fetched (or
    already stored), a parent's blended rate ends up between the two per-day rates."""
    sheet = {
        "B01": {"asin": "B01", "name": "Chana Sattu", "weight": 1.0, "brand": "Mithila Foods",
                "active": True},
    }
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _fake_catalogue_fn(sheet))

    def _econ_row(units):
        return {"childAsin": "B01", "sales": {"unitsOrdered": units, "netUnitsSold": units}}

    calls = []

    async def _fake_fetch_economics(*, days, sleep=None, **_kwargs):
        calls.append(days)
        if days == 30:
            return [_econ_row(300)], [], "2026-08-02", "2026-08-31"   # 10 kg/day
        return [_econ_row(140)], [], "2026-08-25", "2026-08-31"       # 20 kg/day

    monkeypatch.setattr("app.portfolio.economics.fetch_economics", _fake_fetch_economics)

    async def _fake_save_snapshot(db_, start, end, rows):
        return len(rows)

    monkeypatch.setattr("app.portfolio.repository.save_snapshot", _fake_save_snapshot)

    async def _fake_load_snapshot(db_, window):
        if window == ("2026-08-02", "2026-08-31"):
            return [_econ_row(300)]
        return [_econ_row(140)]

    monkeypatch.setattr("app.portfolio.repository.load_snapshot", _fake_load_snapshot)

    async def _fake_windows_available(db_, limit=12):
        return []  # nothing cached, so both windows are fetched

    monkeypatch.setattr("app.portfolio.repository.windows_available", _fake_windows_available)

    result = await refresh.run(db, sleep=_no_sleep)

    assert result["error"] is None
    assert sorted(calls) == [7, 30], "both windows must be requested"
    rows = await repository.load_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["thirty_day_rate"] == 10.0
    assert row["seven_day_rate"] == 20.0
    # 0.4*20 + 0.6*10 = 14.0, the default weight
    assert row["daily_rate"] == 14.0
    assert row["diverged"] is True


async def test_run_reuses_an_already_stored_window_without_refetching(db, monkeypatch):
    """`windows_available` says the 30-day window is already stored — the job must not spend a
    ~2-minute Data Kiosk query for data it already has.

    **`today` is pinned explicitly**, and this is not incidental: `_ensure_window` must check
    `economics.window_for(today, days)` against the cache BEFORE calling `fetch_economics` at
    all, which means it computes real dates from the real clock outside of any mocked function.
    Leaving `today` to default to the real `date.today()` would make the 30-day window this test
    expects to be "already cached" (2026-08-02..2026-08-31) match only on one specific real-world
    date and silently do the wrong thing — fetching unnecessarily — every other day the suite
    runs. Pinning `today` is what makes the cache-hit path exercised deterministically.
    """
    from datetime import date

    sheet = {"B01": {"asin": "B01", "name": "Chana Sattu", "weight": 1.0,
                     "brand": "Mithila Foods", "active": True}}
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _fake_catalogue_fn(sheet))

    async def _fake_windows_available(db_, limit=12):
        return [{"start": "2026-08-02", "end": "2026-08-31", "rows": 1}]

    monkeypatch.setattr("app.portfolio.repository.windows_available", _fake_windows_available)

    fetch_calls = []

    async def _fake_fetch_economics(*, days, sleep=None, **_kwargs):
        fetch_calls.append(days)
        return [], [], "2026-08-25", "2026-08-31"

    monkeypatch.setattr("app.portfolio.economics.fetch_economics", _fake_fetch_economics)

    async def _fake_save_snapshot(db_, start, end, rows):
        return len(rows)

    monkeypatch.setattr("app.portfolio.repository.save_snapshot", _fake_save_snapshot)

    async def _fake_load_snapshot(db_, window):
        return [{"childAsin": "B01", "sales": {"unitsOrdered": 30, "netUnitsSold": 30}}]

    monkeypatch.setattr("app.portfolio.repository.load_snapshot", _fake_load_snapshot)

    # 2026-09-01 -> window_for(_, 30) == ("2026-08-02", "2026-08-31"), matching the "already
    # cached" fixture above exactly; window_for(_, 7) == ("2026-08-25", "2026-08-31"), which the
    # fixture does NOT list as cached, so only that one must trigger fetch_economics.
    await refresh.run(db, sleep=_no_sleep, today=date(2026, 9, 1))
    assert fetch_calls == [7], "the 30-day window was refetched despite already being stored"


async def test_run_records_a_failed_fetch_without_touching_existing_rows(db, monkeypatch):
    """A failed or partial fetch must not overwrite good data — the same discipline the
    ads_refresh table enforces."""
    from app.projections import repository as proj_repo

    await proj_repo.save_row(db, "Chana Sattu", {"daily_rate": 5.0}, source="sheet")

    async def _fake_catalogue():
        return (
            {"B01": {"asin": "B01", "name": "Chana Sattu", "weight": 1.0,
                     "brand": "Mithila Foods", "active": True}},
            None, "sheet",
        )
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _fake_catalogue)

    async def _fake_windows_available(db_, limit=12):
        return []

    monkeypatch.setattr("app.portfolio.repository.windows_available", _fake_windows_available)

    from app.shipment.spapi import SpApiError

    async def _fake_fetch_economics(*, days, sleep=None, **_kwargs):
        raise SpApiError("Amazon credentials are not configured.")

    monkeypatch.setattr("app.portfolio.economics.fetch_economics", _fake_fetch_economics)

    result = await refresh.run(db, sleep=_no_sleep)

    assert result["error"] is not None
    rows = await proj_repo.load_rows(db)
    assert rows[0]["daily_rate"] == 5.0, "the failed fetch overwrote the previous good rate"

    last = await proj_repo.last_refresh(db)
    assert last["error"] is not None


async def test_run_reads_the_saved_blend_weight_not_the_hardcoded_default(db, monkeypatch):
    from app.projections import logic, repository as proj_repo

    await proj_repo.save_blend_settings(db, {"seven_day_weight": 0.8})

    sheet = {"B01": {"asin": "B01", "name": "Chana Sattu", "weight": 1.0,
                     "brand": "Mithila Foods", "active": True}}
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _fake_catalogue_fn(sheet))

    async def _fake_windows_available(db_, limit=12):
        return []

    monkeypatch.setattr("app.portfolio.repository.windows_available", _fake_windows_available)

    def _econ_row(units):
        return {"childAsin": "B01", "sales": {"unitsOrdered": units, "netUnitsSold": units}}

    async def _fake_fetch_economics(*, days, sleep=None, **_kwargs):
        return ([_econ_row(300)], [], "2026-08-02", "2026-08-31") if days == 30 else \
               ([_econ_row(140)], [], "2026-08-25", "2026-08-31")

    monkeypatch.setattr("app.portfolio.economics.fetch_economics", _fake_fetch_economics)

    async def _fake_save_snapshot(db_, start, end, rows):
        return len(rows)
    monkeypatch.setattr("app.portfolio.repository.save_snapshot", _fake_save_snapshot)

    async def _fake_load_snapshot(db_, window):
        return [_econ_row(300)] if window[1].endswith("-31") and window[0].endswith("-02") \
            else [_econ_row(140)]
    monkeypatch.setattr("app.portfolio.repository.load_snapshot", _fake_load_snapshot)

    await refresh.run(db, sleep=_no_sleep)

    rows = await proj_repo.load_rows(db)
    # 0.8*20 + 0.2*10 = 18.0, using the SAVED 0.8 weight, not the DEFAULT_BLEND 0.4
    assert rows[0]["daily_rate"] == 18.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_projections_refresh.py -q -p no:randomly`
Expected: `ModuleNotFoundError: No module named 'app.projections.refresh'`.

- [ ] **Step 3: Write `app/projections/refresh.py`**

```python
"""The weekly 7d/30d sales recompute. **No new Amazon integration** — reuses
`app.portfolio.economics.fetch_economics` and `app.portfolio.repository.save_snapshot`/
`load_snapshot`/`windows_available` exactly as the Portfolio tab's own nightly refresh does.

**A failed or partial fetch must not overwrite good data.** Every parent's existing row is left
untouched if either window's fetch raises — the same discipline `app.ads.refresh`'s
`ads_refresh` table enforces: a record of the failure is kept (`repository.record_refresh`), but
nothing already stored is silently replaced with a wrong number.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import ist
from app.portfolio import economics
from app.portfolio import repository as portfolio_repository
from app.projections import logic, repository
from app.shipment import catalogue
from app.shipment.spapi import SpApiError

logger = logging.getLogger(__name__)


async def _ensure_window(
    db: AsyncSession, days: int, *, sleep, today: date,
) -> tuple[str, str]:
    """Ensure an economics window is stored, and return `(start, end)`. **Checks the cache
    BEFORE fetching, not after** — the whole reason `windows_available` is consulted at all is
    to avoid a ~2-minute Data Kiosk query for a window the Portfolio tab's own nightly refresh
    (or a previous run of this job) already stored. Calling `fetch_economics` unconditionally
    and only skipping the SAVE would still pay the fetch cost every time, which defeats the
    entire point of sharing the cache with the Portfolio tab.

    `economics.window_for(today, days)` is the same pure calculation `fetch_economics` uses
    internally to turn "the last N days" into concrete dates — calling it here costs nothing and
    is what makes the cache check possible before any Amazon call. `today` is a REQUIRED
    parameter, not a default of `date.today()`, so a test can pin it and exercise the cache-hit
    path deterministically rather than depending on which real-world date the suite happens to
    run on.
    """
    start, end = economics.window_for(today, days)
    cached = await portfolio_repository.windows_available(db, limit=50)
    if any(w["start"] == start and w["end"] == end for w in cached):
        return start, end

    asin_rows, _sku_rows, start, end = await economics.fetch_economics(
        days=days, sleep=sleep, today=today,
    )
    await portfolio_repository.save_snapshot(db, start, end, asin_rows)
    return start, end


async def run(db: AsyncSession, *, sleep=asyncio.sleep, today: date | None = None) -> dict:
    """Recompute every sheet-sourced parent row's blended daily rate. Returns
    `{"rows_stored": int, "error": str | None, "window_start": str, "window_end": str}`.

    **Checks `windows_available` before fetching**, so a 30-day window the Portfolio tab's own
    nightly refresh already stored costs nothing extra here — the two features share one cache.

    `today` defaults to the IST calendar day (`app.ist.today()`), never the server's raw UTC
    `date.today()` — this codebase has shipped the IST/UTC boundary bug six separate times (see
    `app/ist.py`'s own docstring), and "which day's window is this" is exactly the kind of
    decision that bug class breaks. A caller (a test) may pin it explicitly.
    """
    if today is None:
        today = ist.today()
    started = datetime.utcnow()
    try:
        sheet_products, _warning, _source = await catalogue.load_catalogue()
        groups = logic.group_active_by_name(sheet_products)

        thirty_start, thirty_end = await _ensure_window(db, 30, sleep=sleep, today=today)
        seven_start, seven_end = await _ensure_window(db, 7, sleep=sleep, today=today)

        thirty_rows = await portfolio_repository.load_snapshot(db, (thirty_start, thirty_end))
        seven_rows = await portfolio_repository.load_snapshot(db, (seven_start, seven_end))

        kg_30 = logic.sales_kg_by_parent(thirty_rows, groups)
        kg_7 = logic.sales_kg_by_parent(seven_rows, groups)

        blend = await repository.load_blend_settings(db)
        weight = blend["seven_day_weight"]
        divergence_fraction = blend["divergence_pct"] / 100

        to_write = []
        for name in groups:
            thirty_kg = kg_30.get(name, 0.0)
            # `name in kg_7` is the only check that matters: `sales_kg_by_parent` returns an
            # entry for a parent the moment ANY of its ASINs has an economics row in that
            # window — including a row reporting 0 units — so "absent from kg_7" means no
            # snapshot row exists at all (pass None) and "present with value 0.0" means a
            # genuine zero-sales week (pass 0.0). See logic.blended_daily_rate's own docstring
            # for why the two must stay distinguishable.
            seven_kg = kg_7[name] if name in kg_7 else None

            rate, diverged = logic.blended_daily_rate(
                thirty_kg, seven_kg, weight, divergence_fraction=divergence_fraction,
            )
            to_write.append({
                "parent_product": name,
                "thirty_day_rate": round(thirty_kg / 30, 2),
                "seven_day_rate": None if seven_kg is None else round(seven_kg / 7, 2),
                "daily_rate": rate,
                "diverged": diverged,
                "last_month_sale": round(thirty_kg, 2),
            })

        written = await repository.upsert_sheet_rows(db, to_write)
        await repository.record_refresh(
            db, window_start=thirty_start, window_end=thirty_end, rows_stored=written,
            started_at=started,
        )
        return {"rows_stored": written, "error": None,
                "window_start": thirty_start, "window_end": thirty_end}

    except SpApiError as exc:
        logger.warning("projections refresh failed: %s", exc)
        await repository.record_refresh(
            db, window_start=None, window_end=None, rows_stored=0, error=str(exc),
            started_at=started,
        )
        return {"rows_stored": 0, "error": str(exc), "window_start": None, "window_end": None}
    except Exception as exc:  # noqa: BLE001 - the screen must say something rather than hang
        logger.exception("projections refresh crashed")
        await repository.record_refresh(
            db, window_start=None, window_end=None, rows_stored=0,
            error=f"Unexpected error: {exc}", started_at=started,
        )
        return {"rows_stored": 0, "error": f"Unexpected error: {exc}",
                "window_start": None, "window_end": None}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_refresh.py -q -p no:randomly`
Expected: all 4 pass.

- [ ] **Step 5: Add the scheduled job to `app/scheduler.py`**

Read the file at `app/scheduler.py` around lines 244-297 (the `PORTFOLIO_REFRESH_IST` /
`ADS_REFRESH_IST` block) to confirm nothing has changed there since this plan was written, then
insert a new block immediately after the `scheduled_ads_refresh` function (after its closing
`logger.info(...)` call, before `async def scheduled_order_refresh`):

```python
#: **07:00 IST, before both the portfolio pull (07:30) and the ads one (08:00)** — see
#: `PORTFOLIO_REFRESH_IST` for why these are stated in IST rather than as a bare server hour.
#: Weekly, not nightly: the owner asked for weekly, and a 30-day rolling average moves little
#: day to day, so a tighter cadence buys nothing. Sunday (day_of_week=6 in APScheduler's
#: 0=Monday..6=Sunday) is an arbitrary but stable choice — a fixed day matters more than which one.
PROJECTIONS_REFRESH_IST = (7, 0)
PROJECTIONS_REFRESH_DAY = 6


async def scheduled_projections_refresh():
    """Recompute every parent's blended 7d/30d sales rate, once a week.

    Reuses the Portfolio tab's own economics fetch — see `app.projections.refresh.run` — so this
    costs nothing extra when the nightly Portfolio refresh has already stored the current 30-day
    window; only the 7-day one is genuinely new most weeks.

    **This job never edits Amazon.** It recomputes a forecast number a human orders against; it
    does not place an order. Same reasoning `scheduled_ads_refresh` documents for why a scheduled
    job here is safe unattended.
    """
    if not get_settings().spapi_configured:
        logger.debug("Projections refresh skipped: SP-API is not configured")
        return

    from app.database import async_session
    from app.projections import refresh as projections_refresh

    async with async_session() as db:
        result = await projections_refresh.run(db)
    if result.get("error"):
        logger.warning("Projections refresh failed: %s", result["error"])
    else:
        logger.info(
            "Projections refresh: %d row(s) for %s..%s",
            result.get("rows_stored", 0), result.get("window_start"), result.get("window_end"),
        )
```

Then in `setup_scheduler()`, immediately after the `ads_utc = ist.utc_hhmm(*ADS_REFRESH_IST)`
block's `parts.append(f"ads at {ist.label(*ADS_REFRESH_IST)}")` line, add:

```python
    # Same flag pair again — a weekly forecast recompute is as safe unattended as the nightly
    # portfolio and ads pulls; none of the three ever writes to Amazon.
    projections_utc = ist.utc_hhmm(*PROJECTIONS_REFRESH_IST)
    scheduler.add_job(
        scheduled_projections_refresh,
        CronTrigger(
            day_of_week=PROJECTIONS_REFRESH_DAY, hour=projections_utc[0], minute=projections_utc[1],
        ),
        id="projections_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    parts.append(f"projections weekly at {ist.label(*PROJECTIONS_REFRESH_IST)}")
```

- [ ] **Step 6: Update the exact-set scheduler test**

Open `tests/test_retention_and_scheduler.py`. Find `test_all_scheduled_jobs_are_registered`
(asserts `set(jobs) ==` a literal set of job ids) and add the new id:

```python
def test_all_scheduled_jobs_are_registered(monkeypatch):
    jobs = _registered_jobs(monkeypatch, hour=6)
    assert set(jobs) == {
        "daily_product_scrape", "daily_keyword_track", "daily_history_purge",
        "order_refresh", "portfolio_refresh", "ads_refresh", "projections_refresh",
    }
```

- [ ] **Step 7: Add an IST-time test for the new job, mirroring the existing ads/portfolio one**

Append to `tests/test_retention_and_scheduler.py`, immediately after
`test_the_nightly_jobs_fire_at_the_IST_time_they_claim`:

```python
def test_the_projections_job_fires_at_07_00_IST_and_before_portfolio(monkeypatch):
    """Same lesson, same test shape as the ads/portfolio pair: assert the IST constant, never
    a hardcoded UTC hour — a literal `hour='1'` here would pin the arithmetic instead of the
    intent, exactly the mistake that shipped the original 09:20 IST bug."""
    from app import ist as ist_module
    from app import scheduler as sched

    jobs = _registered_jobs(monkeypatch, hour=6)

    assert sched.PROJECTIONS_REFRESH_IST == (7, 0)
    proj_hour, proj_minute = ist_module.utc_hhmm(*sched.PROJECTIONS_REFRESH_IST)
    assert f"hour='{proj_hour}'" in jobs["projections_refresh"]
    assert f"minute='{proj_minute}'" in jobs["projections_refresh"]
    assert f"day_of_week='{sched.PROJECTIONS_REFRESH_DAY}'" in jobs["projections_refresh"]

    assert sched.PROJECTIONS_REFRESH_IST < sched.PORTFOLIO_REFRESH_IST, (
        "the weekly recompute must stay before the nightly portfolio pull, or the two "
        "multi-minute Amazon reports overlap on a 951 MB box"
    )
```

- [ ] **Step 8: Run the scheduler tests**

Run: `venv/Scripts/python -m pytest tests/test_retention_and_scheduler.py -q -p no:randomly`
Expected: all pass.

- [ ] **Step 9: Run the full suite**

Run: `venv/Scripts/python -m pytest -q -p no:randomly`
Expected: zero failures beyond the counts already established by prior tasks.

- [ ] **Step 10: Commit**

```bash
git add app/projections/refresh.py app/scheduler.py tests/test_projections_refresh.py tests/test_retention_and_scheduler.py
git commit -m "feat(projections): weekly 7d/30d recompute job, registered at 07:00 IST"
```

---

## Task 7: `deploy/update-ec2.sh` — detector branch and required-tables entry

**Files:**
- Modify: `deploy/update-ec2.sh`
- Modify: `tests/test_schema_migrations.py`

**Interfaces:**
- Consumes: the `<REV>` migration id from Task 1.

**Why this is its own task, not folded into Task 1:** CLAUDE.md records that a stale detector
branch has stamped production backwards and failed two real deploys. This task exists so it is
never possible to forget — the detector is fixed in the SAME commit as the migration it answers
for, but reviewed as its own checklist item so a fresh reviewer's gate specifically covers it.

- [ ] **Step 1: Add the newest-first branch**

Open `deploy/update-ec2.sh`. Find the comment block starting `# ⚠ EVERY NEW MIGRATION MUST ADD A
BRANCH HERE, NEWEST FIRST.` (around line 340) and the `elif "ads_refresh" in tables:` line
immediately below the `if not tables:` branch (around line 369). Insert a new branch ABOVE the
`ads_refresh` one — the new migration is now the true head:

```python
if not tables:
    print("")                                       # empty: migrate from scratch
elif "projection_row" in tables:
    print("<REV>")                                  # head: projection rows + refresh record
elif "ads_refresh" in tables:
    print("c5e91a3d47b6")                           # per-run ads refresh record
```

Replace `<REV>` with the actual migration id from Task 1.

- [ ] **Step 2: Add the two tables to the required-tables check**

Find the `need = {...}` set in the post-migration Python heredoc (search for
`"ads_performance_daily"` — it is the last real entry before the closing `}`). Add:

```python
        "ads_entity", "ads_rule", "ads_mutation",
        "ads_performance_daily",
        "ads_refresh",
        # The Projections tab's live parent rows and weekly-refresh record.
        "projection_row", "projection_refresh"}
```

- [ ] **Step 3: Run the migration-detector test**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py -q -p no:randomly`
Expected: all pass — this test extracts and RUNS the detector heredoc against a freshly
migrated database and asserts it answers with the true head, so a wrong `<REV>` substitution in
Step 1 fails here rather than in production.

- [ ] **Step 4: Commit**

```bash
git add deploy/update-ec2.sh tests/test_schema_migrations.py
git commit -m "chore(deploy): detector branch and required-tables entry for projection_row"
```

---

## Task 8: `templates/projections.html` — sales source, needs-review, blend settings panel

**Files:**
- Modify: `templates/projections.html` (the whole `<script>` body and the relevant markup —
  see the exact replacement below)

**Interfaces:**
- Consumes: the `GET /projections/last` response shape from Task 5
  (`{products, catalogue, blend, last_refresh}`), where each product now carries
  `parent_product` (not `product`), `sales_source`, `needs_review`, `diverged`,
  `seven_day_rate`, `thirty_day_rate`. Also `GET/POST /projections/blend-settings`,
  `POST /projections/reset-row`, `POST /projections/refresh-now`,
  `DELETE /projections/clear` (repurposed meaning — see Task 5's note).

This task has no automated test of its own — `templates/*.html` are exercised by
`tests/test_local_dates.py` (an existing repo-wide source scan) and by the manual verification in
Task 9. Confirm the template still parses as valid Jinja2/HTML after editing (Step 4).

- [ ] **Step 1: Replace the CSV-upload/globals card and add the settings/needs-review UI**

In `templates/projections.html`, replace the two `<div class="globals-card">...</div>` blocks
(the CSV-import card starting `<!-- Global Defaults -->` and the one immediately after it
starting `<label>Growth Rate...`) with:

```html
<!-- Catalogue & refresh status -->
<div class="globals-card" style="flex-direction:column;align-items:stretch;gap:10px">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <strong style="font-size:13px">📋 Active parents</strong>
    <span id="catalogue-summary" style="font-size:12px;color:var(--text-muted)"></span>
    <span id="last-refresh-note" style="font-size:12px;color:var(--text-muted);margin-left:auto"></span>
    <button class="btn-outline" onclick="refreshNow()" id="refresh-now-btn">🔄 Refresh sales now</button>
    <button class="btn-outline" onclick="openBlendSettings()">⚙ Blend settings</button>
  </div>
  <div id="hidden-parents-note" style="font-size:11.5px;color:var(--text-muted);display:none"></div>
</div>

<!-- Blend settings panel, hidden until opened -->
<div class="globals-card" id="blend-panel" style="display:none;flex-direction:column;align-items:stretch;gap:10px">
  <strong style="font-size:13px">7-day / 30-day sales blend</strong>
  <p style="font-size:11.5px;color:var(--text-muted)" id="blend-help"></p>
  <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
    <label style="font-size:12px;color:var(--text-muted);display:flex;flex-direction:column;gap:4px">
      7-day weight (0-1)
      <input type="number" id="blend-weight" step="0.05" min="0" max="1" style="width:80px;background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--text);padding:5px 8px"/>
    </label>
    <label style="font-size:12px;color:var(--text-muted);display:flex;flex-direction:column;gap:4px">
      Divergence flag threshold (%)
      <input type="number" id="blend-threshold" step="5" min="1" max="200" style="width:80px;background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--text);padding:5px 8px"/>
    </label>
    <button class="btn" onclick="saveBlendSettings()">Save</button>
    <button class="btn-outline" onclick="resetBlendSettings()">↺ Reset to defaults</button>
  </div>
</div>

<!-- CSV Upload (manual override) -->
<div class="globals-card" style="flex-direction:column;align-items:stretch;gap:12px">
  <div style="display:flex;align-items:center;gap:12px">
    <strong style="font-size:13px">📂 Manual sales override</strong>
    <span style="font-size:11px;color:var(--text-muted)">Upload a Business Report CSV to override "Last Month Sale" by hand — an overridden row is skipped by the weekly automatic recompute until you reset it.</span>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    <input type="file" id="csv-file" accept=".csv" style="font-size:12px;color:var(--text-muted)"/>
    <button class="btn" onclick="uploadCSV()" id="csv-btn" disabled>Import Sales from CSV</button>
    <span id="csv-spin" style="display:none"><span class="spinner"></span></span>
    <span id="csv-result" style="font-size:12px;color:var(--green)"></span>
  </div>
</div>

<div class="globals-card">
  <div style="display:flex;gap:8px;margin-left:auto;align-items:center">
    <button class="btn" onclick="recalcAll()">🔄 Save edits &amp; Recalculate</button>
    <button class="btn-outline" onclick="downloadExcel()">⬇ Excel</button>
    <button class="btn-danger" onclick="clearAllOverrides()">Reset all manual edits</button>
  </div>
</div>
```

Note what was removed: the seven Global Defaults inputs (`g-growth`, `g-seasonal`, `g-s2w`,
`g-pack`, `g-w2i`, `g-i2f`, `g-wh-buf`) and the "Reset to Defaults"/"Clear" buttons tied to them.
Those inputs applied ONE growth/seasonal/lead-time set to every unconfigured row client-side; that
concept now lives server-side as `GLOBAL_DEFAULTS` in `app/routers/projections.py` (Task 5) and
is applied automatically to any `needs_review` row, with no separate global-editing UI needed —
each row's own inputs (still present in the table, see Step 2) already let the owner override a
single parent's lead times directly.

- [ ] **Step 2: Add the two new table columns**

In the `<thead><tr>` block, add two columns after the existing `<th style="text-align:left">Brand</th>`:

```html
  <th style="text-align:left">Brand</th>
  <th style="text-align:left">Source</th>
  <th>7d / 30d<br/>(kg/day)</th>
```

- [ ] **Step 3: Replace the whole `<script>` body**

Replace everything from `let products = [];` down to (but not including) the closing
`</script>` tag with:

```html
<script>
let products = [];
let currentBrand = "all";
let catalogueInfo = {};
let blendSettings = {};

(async()=>{
  await loadAll();
})();

async function loadAll(){
  const r = await fetch("/projections/last");
  const data = await r.json();
  products = data.products || [];
  catalogueInfo = data.catalogue || {};
  blendSettings = data.blend || {};
  renderCatalogueSummary();
  renderLastRefresh(data.last_refresh);
  showSummary(summaryFromProducts(products));
  renderTable();
}

function summaryFromProducts(list){
  const total_forecast_kg = list.reduce((s,p)=>s+(p.monthly_forecast||0),0);
  const total_ideal_value = list.reduce((s,p)=>s+(p.ideal_stock_value||0),0);
  const total_current_value = list.reduce((s,p)=>s+(p.current_stock_value||0),0);
  return {
    total_products: list.length,
    total_forecast_kg,
    total_ideal_value,
    total_current_value,
    shipment_alerts: list.filter(p=>p.shipment_alert>0).length,
    reorder_alerts: list.filter(p=>p.reorder_alert>0).length,
    critical_alerts: list.filter(p=>(p.inventory_days??999)<7).length,
  };
}

function renderCatalogueSummary(){
  const el = document.getElementById("catalogue-summary");
  const c = catalogueInfo;
  el.textContent = `${c.active_parents||0} active (source: ${c.source||"?"})`;
  if(c.warning){el.textContent += " — " + c.warning;}
  const hiddenEl = document.getElementById("hidden-parents-note");
  if((c.hidden_count||0) > 0){
    hiddenEl.style.display = "block";
    const names = (c.hidden_names||[]).join(", ");
    const more = c.hidden_count > (c.hidden_names||[]).length ? ` and ${c.hidden_count - c.hidden_names.length} more` : "";
    hiddenEl.textContent = `${c.hidden_count} parent(s) hidden — not active in the MRP sheet: ${names}${more}`;
  } else {
    hiddenEl.style.display = "none";
  }
}

function renderLastRefresh(last){
  const el = document.getElementById("last-refresh-note");
  if(!last){el.textContent = "Sales have never been refreshed from Amazon.";return;}
  const when = last.finished_at ? new Date(last.finished_at).toLocaleString("en-IN") : "?";
  if(last.error){
    el.textContent = `Last weekly refresh FAILED (${when}): ${last.error}`;
    el.style.color = "var(--red)";
  } else {
    el.textContent = `Sales last refreshed ${when} (${last.rows_stored} row(s))`;
    el.style.color = "var(--text-muted)";
  }
}

async function refreshNow(){
  const btn = document.getElementById("refresh-now-btn");
  btn.disabled = true;
  btn.textContent = "Refreshing… (can take several minutes)";
  try{
    const r = await fetch("/projections/refresh-now", {method:"POST"});
    const data = await r.json();
    if(data.error) throw new Error(data.error);
    toast(`Refreshed ${data.rows_stored} row(s)`, "success");
    await loadAll();
  }catch(e){
    toast(e.message, "error");
  }finally{
    btn.disabled = false;
    btn.textContent = "🔄 Refresh sales now";
  }
}

// ── Blend settings panel ──
function openBlendSettings(){
  const panel = document.getElementById("blend-panel");
  panel.style.display = panel.style.display === "none" ? "flex" : "none";
  if(panel.style.display !== "none") loadBlendSettings();
}

async function loadBlendSettings(){
  const r = await fetch("/projections/blend-settings");
  const data = await r.json();
  document.getElementById("blend-weight").value = data.blend.seven_day_weight;
  document.getElementById("blend-threshold").value = data.blend.divergence_pct;
  document.getElementById("blend-help").textContent =
    data.help.seven_day_weight + " " + data.help.divergence_pct;
}

async function saveBlendSettings(){
  const weight = parseFloat(document.getElementById("blend-weight").value);
  const threshold = parseFloat(document.getElementById("blend-threshold").value);
  const r = await fetch("/projections/blend-settings", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({blend: {seven_day_weight: weight, divergence_pct: threshold}}),
  });
  const data = await r.json();
  if(data.error){toast(data.error, "error");return;}
  toast("Blend settings saved", "success");
}

async function resetBlendSettings(){
  const r = await fetch("/projections/blend-settings", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({reset: true}),
  });
  const data = await r.json();
  document.getElementById("blend-weight").value = data.blend.seven_day_weight;
  document.getElementById("blend-threshold").value = data.blend.divergence_pct;
  toast("Reset to measured defaults", "success");
}

async function recalcAll(){
  collectFromTable();
  const r = await fetch("/projections/calculate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({products})});
  const data = await r.json();
  products = data.products;
  showSummary(data.summary);
  renderTable();
  toast("Saved "+products.length+" edited row(s)","success");
}

function collectFromTable(){
  const rows = document.querySelectorAll("tbody tr");
  rows.forEach(tr=>{
    const idx = parseInt(tr.dataset.idx);
    if(isNaN(idx)||!products[idx]) return;
    const p = products[idx];
    const get = id => {const el=tr.querySelector(`[data-field="${id}"]`);return el?parseFloat(el.value)||0:p[id]||0;};
    p.last_month_sale = get("last_month_sale");
    p.seasonal_impact = get("seasonal_impact");
    p.growth_rate = get("growth_rate");
    p.supplier_to_wh = get("supplier_to_wh");
    p.packing = get("packing");
    p.wh_to_ixd = get("wh_to_ixd");
    p.ixd_to_fba = get("ixd_to_fba");
    p.current_fba_stock = get("current_fba_stock");
    p.current_wh_stock = get("current_wh_stock");
    p.wh_buffer_days = get("wh_buffer_days");
    p.purchase_rate = get("purchase_rate");
  });
}

function showSummary(s){
  if(!s)return;
  const fmt = n=>"₹"+Number(n).toLocaleString("en-IN",{maximumFractionDigits:0});
  document.getElementById("summary").style.display="grid";
  document.getElementById("summary").innerHTML=`
    <div class="stat"><div class="num">${s.total_products}</div><div class="lbl">Products</div></div>
    <div class="stat"><div class="num">${Number(s.total_forecast_kg).toLocaleString("en-IN",{maximumFractionDigits:0})}</div><div class="lbl">Forecast (kg)</div></div>
    <div class="stat"><div class="num">${fmt(s.total_ideal_value)}</div><div class="lbl">Ideal Stock Value</div></div>
    <div class="stat"><div class="num">${fmt(s.total_current_value)}</div><div class="lbl">Current Stock Value</div></div>
    <div class="stat ${s.shipment_alerts>0?'alert':''}"><div class="num">${s.shipment_alerts}</div><div class="lbl">Shipment Alerts</div></div>
    <div class="stat ${s.reorder_alerts>0?'warn':''}"><div class="num">${s.reorder_alerts}</div><div class="lbl">Reorder Alerts</div></div>
    <div class="stat ${s.critical_alerts>0?'alert':''}"><div class="num">${s.critical_alerts}</div><div class="lbl">Critical (&lt;7d)</div></div>`;
}

function setBrand(b,el){currentBrand=b;document.querySelectorAll('.chip[data-brand]').forEach(c=>c.classList.remove("active"));el.classList.add("active");renderTable();}

async function resetRow(parentProduct){
  const r = await fetch("/projections/reset-row", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({parent_product: parentProduct}),
  });
  if(r.ok){toast(`${parentProduct}: reset to live sheet data`,"success");await loadAll();}
}

/* Delegated, not an inline onclick — `parent_product` comes from the MRP sheet, which is
   hand-edited by someone other than a developer, and building an onclick out of it is exactly
   the injection risk CLAUDE.md already rules out for keyword text and campaign names on the
   Ads and Portfolio tabs. The button's own value carries the name instead. */
document.getElementById("tbody").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-reset-row]");
  if(btn) resetRow(btn.dataset.resetRow);
});

async function clearAllOverrides(){
  if(!confirm("Reset every manually-edited row back to live sheet data?"))return;
  const r = await fetch("/projections/clear",{method:"DELETE"});
  const data = await r.json();
  toast(`Reset ${data.reset_count} manual override(s)`,"success");
  await loadAll();
}

function renderTable(){
  const q=(document.getElementById("search").value||"").toLowerCase();
  let filtered=products.map((p,i)=>({...p,_idx:i})).filter(p=>{
    if(currentBrand!=="all"&&p.brand!==currentBrand)return false;
    if(q&&!p.parent_product.toLowerCase().includes(q))return false;
    return true;
  });
  document.getElementById("count-lbl").textContent=filtered.length+" of "+products.length;
  const tbody=document.getElementById("tbody");
  if(!filtered.length){tbody.innerHTML="";document.getElementById("empty").style.display="block";return;}
  document.getElementById("empty").style.display="none";

  const fmt=n=>"₹"+Number(n).toLocaleString("en-IN",{maximumFractionDigits:0});
  tbody.innerHTML=filtered.map((p,i)=>{
    const shipCls=p.shipment_alert>0?"alert-pos":"alert-neg";
    const reorderCls=p.reorder_alert>0?"alert-pos":"alert-neg";
    const invCls=p.inventory_days<7?"inv-critical":p.inventory_days<14?"inv-warning":"inv-good";
    const leadTotal=(p.supplier_to_wh||0)+(p.packing||0)+(p.wh_to_ixd||0)+(p.ixd_to_fba||0);
    const nameCell = p.needs_review
      ? `${escHtml(p.parent_product)} <span title="No saved purchasing config matched this product — using Global Defaults. Check the purchase rate and lead times." style="color:var(--orange);font-weight:700">⚠ needs review</span>`
      : escHtml(p.parent_product);
    const sourceCell = p.sales_source === "manual"
      ? `manual <button class="btn-outline" style="padding:2px 6px;font-size:10px" data-reset-row="${escHtml(p.parent_product)}">reset</button>`
      : "sheet";
    const rateCell = p.seven_day_rate == null
      ? `— / ${(p.thirty_day_rate||0).toFixed(1)}`
      : `${p.diverged ? '<span title="7-day and 30-day rates disagree beyond the saved threshold" style="color:var(--orange)">⚠</span> ' : ''}${p.seven_day_rate.toFixed(1)} / ${(p.thirty_day_rate||0).toFixed(1)}`;
    return `<tr data-idx="${p._idx}">
      <td style="color:var(--text-muted);font-size:11px">${i+1}</td>
      <td class="product-name">${nameCell}</td>
      <td class="brand-cell">${escHtml(p.brand||"")}</td>
      <td class="brand-cell" style="font-size:10.5px">${sourceCell}</td>
      <td style="font-size:10.5px;color:var(--text-muted)">${rateCell}</td>
      <td><input data-field="last_month_sale" value="${p.last_month_sale||0}" class="wide"/></td>
      <td><input data-field="seasonal_impact" value="${p.seasonal_impact||1}" style="width:45px"/></td>
      <td><input data-field="growth_rate" value="${p.growth_rate||0.3}" style="width:45px"/></td>
      <td><strong>${p.monthly_forecast?Math.round(p.monthly_forecast):0}</strong></td>
      <td style="color:var(--text-muted)">${p.daily_rate?p.daily_rate.toFixed(1):0}</td>
      <td><input data-field="supplier_to_wh" value="${p.supplier_to_wh||5}" style="width:35px"/></td>
      <td><input data-field="packing" value="${p.packing||2}" style="width:35px"/></td>
      <td><input data-field="wh_to_ixd" value="${p.wh_to_ixd||10}" style="width:35px"/></td>
      <td><input data-field="ixd_to_fba" value="${p.ixd_to_fba||5}" style="width:35px"/></td>
      <td style="color:var(--text-muted)">${leadTotal}</td>
      <td><strong>${p.ideal_fba_stock?Math.round(p.ideal_fba_stock):0}</strong></td>
      <td><input data-field="current_fba_stock" value="${p.current_fba_stock||0}" class="wide"/></td>
      <td class="${shipCls}">${p.shipment_alert?Math.round(p.shipment_alert):0}</td>
      <td><input data-field="wh_buffer_days" value="${p.wh_buffer_days||10}" style="width:40px"/></td>
      <td>${p.ideal_wh_stock?Math.round(p.ideal_wh_stock):0}</td>
      <td><input data-field="current_wh_stock" value="${p.current_wh_stock||0}" style="width:55px"/></td>
      <td class="${reorderCls}">${p.reorder_alert?Math.round(p.reorder_alert):0}</td>
      <td><input data-field="purchase_rate" value="${p.purchase_rate||0}" style="width:50px"/></td>
      <td style="font-size:11px">${p.ideal_stock_value?fmt(p.ideal_stock_value):"—"}</td>
      <td class="${invCls}">${p.inventory_days!=null?Math.round(p.inventory_days):0}d</td>
    </tr>`;
  }).join("");
}

function escHtml(s){
  return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function downloadExcel(){window.location.href="/projections/download";}

// CSV Upload — a manual override, not a live source
document.getElementById("csv-file").addEventListener("change",function(){document.getElementById("csv-btn").disabled=!this.files.length;});
async function uploadCSV(){
  const fileInput=document.getElementById("csv-file");
  if(!fileInput.files[0])return;
  document.getElementById("csv-btn").disabled=true;
  document.getElementById("csv-spin").style.display="inline";
  document.getElementById("csv-result").textContent="";
  const fd=new FormData();fd.append("file",fileInput.files[0]);
  try{
    const r=await fetch("/projections/upload-csv",{method:"POST",body:fd});
    const data=await r.json();
    if(data.error) throw new Error(data.error);
    products=data.products;
    showSummary({total_products:data.total_products,total_forecast_kg:data.total_forecast_kg,total_ideal_value:0,total_current_value:0,shipment_alerts:0,reorder_alerts:0,critical_alerts:0});
    renderTable();
    document.getElementById("csv-result").textContent=`Imported ${data.filled_from_csv} products, ${data.total_kg_from_csv} kg total sales`;
    toast(`Sales imported: ${data.filled_from_csv} products filled from CSV, marked manual`,"success");
  }catch(e){toast(e.message,"error");}
  finally{document.getElementById("csv-btn").disabled=false;document.getElementById("csv-spin").style.display="none";}
}

function toast(msg,type=""){const el=document.getElementById("toast");el.textContent=msg;el.className=type;el.style.display="block";setTimeout(()=>el.style.display="none",3500);}
</script>
```

**Note the deliberate changes from the pre-existing script**: `p.product` is now `p.parent_product`
everywhere (matching Task 5's response shape); `initFromDefaults()` and `clearAll()` are gone,
replaced by `loadAll()` (always called on page load — there is no more "never initialised" state)
and `clearAllOverrides()` (resets manual edits rather than deleting a file); the seven
`getGlobals()`-sourced fields are gone from the JS entirely, since Global Defaults now live only
server-side. **The per-row "reset" button is a delegated listener reading `data-reset-row`, not an
inline `onclick`** — `parent_product` comes from the MRP sheet, which CLAUDE.md already treats as
untrusted-enough-to-inject for keyword and campaign text on the Ads and Portfolio tabs, and every
sheet-derived string rendered into the table (`parent_product`, `brand`) goes through `escHtml`,
which now also escapes `"` for the one place a value lands inside a double-quoted attribute
(`data-reset-row`).

- [ ] **Step 4: Confirm the template still renders**

Run:
```bash
venv/Scripts/python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
html = env.get_template('projections.html').render(active='projections', grant=None)
print('rendered', len(html), 'chars')
"
```
Expected: `rendered <N> chars`, no Jinja2 error. (`grant=None` is fine — the template does not
reference `grant` directly; it is only passed by `app/main.py`'s route for consistency with the
nav partial, which reads it from request state, not from this local variable — confirm this by
running the command and checking there is no `UndefinedError`.)

- [ ] **Step 5: Syntax-check the extracted JavaScript**

Run:
```bash
venv/Scripts/python -c "
import re, pathlib, tempfile, os
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
html = env.get_template('projections.html').render(active='projections', grant=None)
m = re.search(r'<script>(.*)</script>', html, re.S)
out = os.path.join(tempfile.gettempdir(), 'projections_check.js')
pathlib.Path(out).write_text(m.group(1), encoding='utf-8')
print(out)
"
```
Then, using the printed path: `node --check <printed path>`
Expected: no output from `node --check` (a clean exit means valid syntax).

- [ ] **Step 6: Commit**

```bash
git add templates/projections.html
git commit -m "feat(projections): sales-source column, needs-review flag, blend settings panel"
```

---

## Task 9: mutation testing — the acceptance bar this codebase already uses

**Files:**
- Create: `scripts/mutate_projections.py`

**Interfaces:** none — this is a standalone script following the exact shape of
`scripts/mutate_coverage.py` and `scripts/mutate_grouping.py`, already in this repo. It is a
throwaway verification tool, never imported by the app or its tests.

**Why this task exists:** this codebase's CLAUDE.md records four separate cases where a real bug
survived a fully green test suite (the Ads Sponsored-Brands daily-grain bug; the ads preview
grouping's one-campaign fixture; the IST test whose pretend date happened to equal the real date
twice). "The tests pass" is not this project's bar; "a deliberate mutation of the decision is
caught" is.

- [ ] **Step 1: Write `scripts/mutate_projections.py`**

```python
"""Mutation harness for the Projections live-data change. Throwaway; not imported by the app.

Each entry breaks ONE decision this feature makes and names the test that must catch it.

    venv/Scripts/python scripts/mutate_projections.py
"""
import pathlib
import subprocess
import sys

LOGIC = pathlib.Path("app/projections/logic.py")
REPO = pathlib.Path("app/projections/repository.py")
REFRESH = pathlib.Path("app/projections/refresh.py")
ROUTER = pathlib.Path("app/routers/projections.py")

MUTATIONS = [
    (
        "normalize_name stops stripping hyphens, so a plausible sheet spelling stops matching",
        LOGIC,
        r'return re.sub(r"[\s-]+", "", name).casefold()',
        r'return re.sub(r"[\s]+", "", name).casefold()',
        "test_normalize_name_ignores_case_space_and_hyphen",
    ),
    (
        "group_active_by_name stops excluding inactive ASINs",
        LOGIC,
        "        if not row.get(\"active\"):\n            continue",
        "        if False:\n            continue",
        "test_group_active_by_name_excludes_inactive_asins",
    ),
    (
        "sales_kg_by_parent reads net_units instead of units_ordered",
        LOGIC,
        'units = int((row.get("sales") or {}).get("unitsOrdered") or 0)',
        'units = int((row.get("sales") or {}).get("netUnitsSold") or 0)',
        "test_sales_kg_by_parent_ignores_net_units_and_never_goes_negative",
    ),
    (
        "blended_daily_rate treats a missing 7-day window as a zero-sales week",
        LOGIC,
        "    if kg_7d is None:\n        return round(rate_30, 2), False",
        "    if kg_7d is None:\n        kg_7d = 0.0",
        "test_blended_daily_rate_falls_back_to_thirty_day_when_seven_day_is_missing",
    ),
    (
        "blend_or_default stops validating a stored value on read",
        LOGIC,
        "        if blend_setting_error(key, value) is None:\n            merged[key] = float(value)",
        "        merged[key] = float(value)",
        "test_blend_or_default_discards_an_invalid_stored_value",
    ),
    (
        "upsert_sheet_rows stops skipping a manually-edited row",
        REPO,
        '        if current is not None and current.sales_source == "manual":\n            continue',
        "        pass",
        "test_upsert_sheet_rows_skips_a_manually_edited_row",
    ),
    (
        "hidden_parent_names stops sorting, so the note's order is arbitrary",
        LOGIC,
        "    return sorted(stored_names - set(live_groups))",
        "    return list(stored_names - set(live_groups))",
        "test_hidden_parent_names_are_stored_parents_absent_from_the_live_groups",
    ),
    (
        "the refresh job stops recording a failed run",
        REFRESH,
        '        await repository.record_refresh(\n'
        '            db, window_start=None, window_end=None, rows_stored=0, error=str(exc),\n'
        '            started_at=started,\n'
        '        )\n'
        '        return {"rows_stored": 0, "error": str(exc), "window_start": None, "window_end": None}',
        '        return {"rows_stored": 0, "error": str(exc), "window_start": None, "window_end": None}',
        "test_run_records_a_failed_fetch_without_touching_existing_rows",
    ),
    (
        "/projections/calculate stops marking a saved row as manual",
        ROUTER,
        '        }, source="manual")',
        '        }, source="sheet")',
        "test_calculate_marks_every_saved_row_manual",
    ),
]


def run(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", expression, "tests"],
        capture_output=True, text=True,
    )
    last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return result.returncode, last


def main():
    survivors = []
    for label, path, old, new, test_name in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP      {label}\n          target text not found in {path}")
            survivors.append((label, f"target text not found in {path}"))
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, summary = run(test_name)
        finally:
            path.write_text(original, encoding="utf-8")
        if code == 0:
            print(f"SURVIVED  {label}\n          {test_name} still passes -> {summary}")
            survivors.append((label, test_name))
        else:
            print(f"caught    {label}\n          {summary}")

    print()
    if survivors:
        print(f"{len(survivors)} SURVIVOR(S):")
        for label, detail in survivors:
            print(f"  - {label} ({detail})")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the harness**

Run: `venv/Scripts/python scripts/mutate_projections.py`
Expected: `all 9 mutations caught`. If anything SURVIVES, read the printed label, find the test
that was supposed to catch it, and strengthen that test's fixture (the common cause in this
codebase, per CLAUDE.md, is a fixture too narrow to discriminate — e.g. only one campaign, or a
pretend date equal to the real one) until it fails against the mutation. Do not weaken the
mutation to make it pass; the mutation encodes a decision made during brainstorming and must stay
representative of a real, plausible bug.

- [ ] **Step 3: Run the full suite one more time**

Run: `venv/Scripts/python -m pytest -q -p no:randomly`
Expected: zero failures.

- [ ] **Step 4: Commit**

```bash
git add scripts/mutate_projections.py
git commit -m "test(projections): mutation harness for the live-data change"
```

---

## Task 10: `CLAUDE.md` — document the change

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a new section**

Find the `## Ads tab` section's end in `CLAUDE.md` (search for `## Known gaps` — the section that
follows Ads) and insert a new top-level section immediately before it:

```markdown
## Projections tab — live parents, live sales, a 7d/30d weighted blend

`/projections-page`. Forecasts next month's kg demand per parent product and flags reorder/
shipment alerts, the same purchasing-planning screen it always was — what changed is where every
number comes from.

### Parents come from the MRP sheet, not a static file — the Triphala Sattu bug, again
The tab used to build every row from `app/invoice/projection_defaults.json`, 81 hand-maintained
parents, filtered by nothing. **Measured before changing it**: only 37 of those 81 have any active
ASIN reachable through `app/invoice/product_families.json`, and Triphala Sattu — active in the
sheet, two pack sizes — was not in that file at all, so no amount of filtering the existing 81
would have shown it. This is the identical defect the Shipment tab already fixed for the same
reason (see "The product list comes from the MRP sheet, live", above), and it needed the same fix
here: `app.shipment.catalogue.load_catalogue()`, the same source and fallback chain, decides which
parents exist. A parent inactive in the sheet is hidden and NAMED (capped at 8) in
`GET /projections/last`'s `catalogue.hidden_names` — never a bare count.

**The parent-grouping unit is the sheet's own product NAME, unmerged — deliberately different
from Portfolio's `family_label()`.** That function merges flavour variants (Cheese & Cream Chana,
Nimbu Pudina Chana, Peri Peri Chana...) into one shared display name for a rollup; `product_
families.json` itself keeps them as separate parents, because different flavours are different
recipes and different purchase decisions. Reusing `family_label()` here would have been the wrong
tool for a purchasing grouping.

**A parent with no matching entry in `projection_defaults.json` gets Global Defaults and is
flagged `needs_review`, never hidden.** Hiding it would repeat the exact mistake this change
exists to fix, with a different static file. Measured: 14 of 38 currently-active parent names had
no match under any spelling, including Triphala Sattu, Makkai Sattu, Raw Flaxseed, and "Bengali
Gobindobhog Rice" — almost certainly the existing "Govind Bhog Rice" under a spelling variant, left
for the owner to merge by hand rather than fuzzy-matched automatically.

### Sales come from `economics_snapshot` — no new Amazon integration
The nightly Portfolio refresh already stores `units_ordered`/`units_refunded`/`net_units` per
child ASIN. The weekly Projections recompute (`app.projections.refresh.run`) reuses
`app.portfolio.economics.fetch_economics` and `app.portfolio.repository.save_snapshot`/
`load_snapshot`/`windows_available` directly — the two features share one cache, so a 30-day
window the Portfolio tab's nightly job already fetched costs nothing extra here.

**`units_ordered`, never `net_units`.** Measured: 2 ASINs in a real 7-day window had
`net_units < 0` (a refund-heavy week), and a negative daily rate would produce a negative
purchase quantity. Refunds are a returns problem, not lower demand.

The pre-existing manual Business Report CSV upload stays, as an explicit override: any row edited
through `/calculate` or `/upload-csv` is marked `sales_source="manual"` in `projection_row`, and
the weekly job skips a manual row entirely until the owner resets it (`POST /reset-row`) — the
same reasoning `ProductDecision` follows for never being touched by an automated pass.

### The forecast blends the last 7 days against the last 30, and a missing window is not a zero
Asked for as *"basis last 7 days sale I want to update the data weekly on some kind of weighted
average of last month and last 7 days... to account for [products that] spike or suddenly drop."*

Measured on the real account: the two windows broadly agree in aggregate (461.1 kg/day 30-day vs
421.1 kg/day 7-day, a 0.91x ratio) while individual parents diverge sharply — Bangla Moori 1.74x,
Flax Seed 1.58x on the way up; Miniket Rice 0.37x, Bangla Roasted Chana 0.43x on the way down. The
blend (`app.projections.logic.blended_daily_rate`, default weight 0.4 toward the 7-day rate) is
exactly what surfaces that.

> **A missing 7-day window and a genuine zero-sales week are different facts, and conflating them
> would have cut real forecasts on no evidence.** Measured: 4 of 47 currently-selling parents had
> 30-day sales but no economics row at all in the 7-day window when this was checked — slow
> movers, not dead ones. `blended_daily_rate` takes `kg_7d: float | None`: `None` (no snapshot row
> exists yet for this parent in that window) falls back to the 30-day rate entirely; `0.0` (the
> window exists and genuinely recorded no sales) IS blended at the normal weight. Collapsing the
> two would cut a slow mover's forecast by the blend weight on every refresh where the 7-day fetch
> simply had not landed for it yet.

**Divergence is flagged, not hidden.** When `|7d rate / 30d rate - 1|` exceeds a saved threshold
(default 30%), the row's `diverged` flag is set and both rates are shown — a forecast number that
moved with no visible cause is what erodes trust in the whole screen, the same principle behind
showing the true bid on the Ads tab rather than silently correcting a stale one.

The blend weight and divergence threshold are saved settings (`portfolio_settings`, name
`projection_blend` — that table is shared and name-keyed across features, not owned by Portfolio
or Ads specifically), **range-checked on read AND write**: the `good_rating: 99` lesson from the
Portfolio tab, where an unvalidated stored threshold silently broke every verdict on the account.

### The weekly refresh runs at 07:00 IST, before Portfolio and Ads
Registered through `app.ist.utc_hhmm`, the same way the Ads and Portfolio jobs are — no bare hour
reaches `CronTrigger`, which is the mistake that put those two jobs at 08:50/09:20 IST for months
before it was found. `day_of_week=6` (Sunday) is arbitrary but fixed; weekly, not nightly, because
a 30-day rolling average moves little day to day and the owner asked for weekly specifically.

**A failed or partial fetch never overwrites a parent's existing good rate.** The same discipline
`ads_refresh` enforces: the failure is recorded (`projection_refresh`, mirroring
`economics_refresh`'s shape) so the screen can say the last attempt failed and when, but nothing
already stored is silently replaced with a wrong number.

### `ProjectionRow` is keyed on the parent NAME, like `ProductRawStock`
Not an ASIN, and not a database id the owner would have to look up: the MRP sheet's own product
name is the one identifier a genuinely new parent (Triphala Sattu) carries from day one, and it is
already how `ProductRawStock` — the Orders tab's raw-material table — is keyed, for the identical
reason (bulk purchasing has no per-pack-size distinction; there is no such thing as 500g-flavoured
raw sattu).
```

- [ ] **Step 2: Confirm the addition doesn't break the CLAUDE.md-scanning tests**

Run: `venv/Scripts/python -m pytest tests/test_local_dates.py tests/test_theme.py -q -p no:randomly`
Expected: all pass (these scan repo-wide patterns; CLAUDE.md prose is not one of their targets,
but confirming avoids a surprise).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the Projections live-data change"
```

---

## Final verification (manual, once every task above is complete)

1. Start the app: use `preview_start` with the `tracker` launch config (per `CLAUDE.md`'s "How to
   Run" section), or `venv/Scripts/python -m uvicorn app.main:app --reload --port 8000`.
2. Sign in, open `/projections-page`.
3. Confirm the "Active parents" summary shows a real count and the source (`sheet`/`cache`/`none`)
   — if the live sheet is reachable it should read `sheet`.
4. If the MRP sheet lists Triphala Sattu as active (confirmed earlier in this project's history
   that it does), find its row and confirm it shows the `⚠ needs review` marker.
5. Confirm the hidden-parents note appears if any previously-stored parent is no longer active,
   naming up to 8 by name.
6. Click "Refresh sales now". Confirm the button disables, then re-enables, and the "last
   refreshed" note updates. This will take several minutes (Data Kiosk fetch) if SP-API is
   configured and the windows are not already cached; if SP-API is not configured, confirm it
   fails with a clear, readable error rather than hanging.
7. Open "Blend settings", change the 7-day weight to something visibly different (e.g. 0.7), Save,
   reload the page, and confirm the row's `daily_rate` values changed accordingly and any row that
   crossed the divergence threshold now shows the `⚠` marker.
8. Reset blend settings; confirm it returns to 0.4/30.
9. Hand-edit one row's "Last Month Sale" and click "Save edits & Recalculate". Confirm that row's
   Source column now reads "manual" with a "reset" button, and every other row still reads "sheet".
10. Click that row's "reset" button. Confirm the Source reverts to "sheet".
11. Download the Excel export and confirm it opens and includes the Sales Source / Needs Review
    columns.
12. Run the full automated suite one final time: `venv/Scripts/python -m pytest -q -p no:randomly`
    — this should be the same command used after every task above, now green end to end.
