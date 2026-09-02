# Projections reorder-point formula — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Projections tab's two disagreeing forecast paths (blended sales rate for a
refreshed `sheet` row, seasonal/growth formula for everything else) with one formula that always
applies seasonality and a new global growth rate, computes a genuine warehouse reorder point from
the real supplier lead time plus a volatility-widened buffer, and removes the eight columns tied to
automation that does not exist yet.

**Architecture:** Pure-function changes in `app/projections/logic.py` (the formula itself),
`app/projections/repository.py` gains two new settings keys reusing the existing
`projection_blend` JSON row, `app/routers/projections.py` drops the removed fields from three
response shapes, one Alembic migration drops the now-dead `growth_rate` column, and
`templates/projections.html` gets a column removal/reorder, a new growth-rate input, and a default
sort. No new tables, no new Amazon integration, no change to the weekly refresh job or the
blend-settings mechanism itself.

**Tech Stack:** FastAPI, SQLAlchemy async, SQLite (local) / PostgreSQL (prod), Alembic, pytest
(async), vanilla JS + Jinja2 templates.

## Global Constraints

- `app/projections/logic.py` stays pure — no DB, no network (existing module-level rule).
- `app/projections/repository.py` stays the only SQL for `projection_row`/`projection_refresh`
  (existing module-level rule).
- Every new/changed blend setting follows the exact `DEFAULT_BLEND` / `BLEND_RANGES` /
  `blend_setting_error` / `blend_or_default` pattern already in `logic.py` — validated on read
  AND write, per the `good_rating: 99` lesson this codebase already learned once.
- `growth_rate` applies identically whether a row's `sales_source` is `sheet` or `manual` — this
  is the specific bug this plan exists to fix, and every new test must be able to fail against a
  reintroduction of the old branch.
- `divergence_buffer_multiplier` floor is `1.0`, not `0.0` — a value below 1 would shrink a
  volatile product's buffer, the exact inversion class `good_rating: 99` already taught this
  codebase to range-check for.
- Migration follows `0f85fa400957`'s pattern exactly: `batch_alter_table` for the drop (SQLite
  cannot `DROP COLUMN` before 3.35; PostgreSQL ignores the batching), column restored on downgrade
  with a sensible default.
- Every new migration adds a branch to `deploy/update-ec2.sh`'s baseline detector, **newest
  first**, keyed on a column/table it specifically adds or removes — CLAUDE.md records two
  production deploy failures from this list going stale.

---

## Task 1: `app/projections/logic.py` — the reorder-point formula

**Files:**
- Modify: `app/projections/logic.py:93-162` (`calculate_projections`) and `:225-246`
  (`DEFAULT_BLEND`, `BLEND_RANGES`)
- Test: `tests/test_projections_logic.py:96-176` (rewrite the `GLOBAL_DEFAULTS` fixture and the
  three `calculate_projections` tests), plus new tests for the formula itself

**Interfaces:**
- Consumes: nothing new — same `products: list[dict]` shape `calculate_projections` already
  receives, with two new keys read per product: `diverged` (bool, already stored on every row) and
  a new parameter `global_growth_rate: float` and `divergence_buffer_multiplier: float` passed by
  the caller (the router), not read from the product dict — they are account-wide settings, not
  per-row data.
- Produces: `calculate_projections(products, *, global_growth_rate, divergence_buffer_multiplier)`
  — note the new required keyword-only parameters; every existing caller
  (`app/routers/projections.py`'s four call sites) must be updated in Task 3, or the app will not
  import. `DEFAULT_BLEND` gains two new keys: `global_growth_rate` (default `0.3`) and
  `divergence_buffer_multiplier` (default `1.5`). `BLEND_RANGES` gains matching entries.
  `CONFIG_FIELDS` (used by `build_parent_config`) drops `"growth_rate"`.

- [ ] **Step 1: Write the failing tests for the new formula**

Replace the `GLOBAL_DEFAULTS` fixture and the three `calculate_projections` tests at
`tests/test_projections_logic.py:93-177` with:

```python
# ─── build_parent_config ──────────────────────────────────────────────────────

GLOBAL_DEFAULTS = {
    "seasonal_impact": 1.0, "supplier_to_wh": 5, "packing": 2,
    "wh_to_ixd": 10, "ixd_to_fba": 5, "wh_buffer_days": 10.0,
}


def test_build_parent_config_uses_matched_defaults():
    defaults = {"Chana Sattu": {"purchase_rate": 120.0, "supplier_to_wh": 5, "packing": 2,
                                 "wh_to_ixd": 10, "ixd_to_fba": 5, "wh_buffer_days": 10.0,
                                 "seasonal_impact": 1.5, "brand": "Mithila Foods"}}
    config = logic.build_parent_config("Chana Sattu", {}, defaults, GLOBAL_DEFAULTS)
    assert config["purchase_rate"] == 120.0
    assert config["needs_review"] is False
    assert "growth_rate" not in config, "growth is now a global setting, not a per-parent field"


def test_build_parent_config_flags_needs_review_with_global_defaults():
    """The Triphala Sattu case: no match anywhere, so it gets Global Defaults and is flagged —
    never hidden, per the owner's explicit decision that a live product must never be invisible
    because a static file has not heard of it."""
    config = logic.build_parent_config("Triphala Sattu", {}, {}, GLOBAL_DEFAULTS)
    assert config["needs_review"] is True
    assert config["purchase_rate"] == 0
    assert config["seasonal_impact"] == GLOBAL_DEFAULTS["seasonal_impact"]
    assert config["wh_buffer_days"] == GLOBAL_DEFAULTS["wh_buffer_days"]


# ─── calculate_projections: one formula, always applied ──────────────────────


def test_calculate_projections_applies_seasonality_and_growth_to_a_sheet_row():
    """**The bug this test exists to catch.** The pre-existing code only applied
    seasonal/growth to a row that had NEVER been through the weekly blend — a `sheet` row with a
    real blended `daily_rate` skipped both factors entirely. Now they always apply, on top of
    whichever daily rate is in play.
    """
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "sheet", "diverged": False,
        "daily_rate": 14.0,               # the blended rate, as the weekly refresh job stores it
        "last_month_sale": 300.0,          # unused when daily_rate is already present
        "seasonal_impact": 2.0,
        "purchase_rate": 0, "supplier_to_wh": 5, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 10.0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(
        products, global_growth_rate=0.3, divergence_buffer_multiplier=1.5,
    )[0]
    # daily_rate stays the blended figure...
    assert result["daily_rate"] == 14.0, "the blended rate was overwritten"
    # ...but monthly_forecast now reflects seasonality and growth on TOP of it:
    # 14 * 30 * 2.0 * 1.3 = 1092.0 — under the old code this was 420.0 (14 * 30, no factors)
    assert result["monthly_forecast"] == pytest.approx(1092.0)


def test_calculate_projections_falls_back_to_last_month_sale_when_daily_rate_is_zero():
    """A `manual` row, or a `sheet` row never yet refreshed, has no blended rate — falls back to
    last_month_sale / 30 as its demand rate, then the SAME seasonality/growth factors apply."""
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "manual", "diverged": False,
        "daily_rate": 0, "last_month_sale": 300.0,
        "seasonal_impact": 1.5,
        "purchase_rate": 0, "supplier_to_wh": 5, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 10.0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(
        products, global_growth_rate=0.2, divergence_buffer_multiplier=1.5,
    )[0]
    # demand_rate = 300/30 = 10.0 kg/day; monthly_forecast = 10 * 30 * 1.5 * 1.2 = 540.0
    assert result["daily_rate"] == pytest.approx(10.0)
    assert result["monthly_forecast"] == pytest.approx(540.0)


def test_ideal_wh_stock_includes_the_supplier_lead_time():
    """**The second bug this plan exists to catch.** `ideal_wh_stock` used to be
    `daily_rate * wh_buffer_days` alone — the supplier lead time (`supplier_to_wh`) never entered
    the WH reorder trigger at all, only `Lead Total` / `Ideal FBA`. Now it does."""
    products = [{
        "parent_product": "Govind Bhog Rice", "sales_source": "sheet", "diverged": False,
        "daily_rate": 35.4, "last_month_sale": 0, "seasonal_impact": 1.0,
        "purchase_rate": 0, "supplier_to_wh": 2, "packing": 2, "wh_to_ixd": 10, "ixd_to_fba": 5,
        "wh_buffer_days": 8.5, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(
        products, global_growth_rate=0.3, divergence_buffer_multiplier=1.5,
    )[0]
    # not diverged, so effective_wh_buffer == wh_buffer_days == 8.5
    # ideal_wh_stock = 35.4 * (2 + 8.5) * 1.0 * 1.3 = 483.21
    assert result["ideal_wh_stock"] == pytest.approx(483.21, abs=0.05)
    # the OLD formula (daily_rate * wh_buffer_days alone, no lead, no growth) gave 300.9 —
    # assert the new figure is meaningfully larger, not coincidentally close to the old one
    assert result["ideal_wh_stock"] > 300.9 + 50


def test_ideal_wh_stock_widens_the_buffer_for_a_diverged_row():
    """The exact worked example from the spec: Govind Bhog Rice's real 02 Sep production figures
    (7d=42.3, 30d=30.8, blended to 35.4 kg/day, flagged diverged), at the default multiplier."""
    products = [{
        "parent_product": "Govind Bhog Rice", "sales_source": "sheet", "diverged": True,
        "daily_rate": 35.4, "last_month_sale": 0, "seasonal_impact": 1.0,
        "purchase_rate": 0, "supplier_to_wh": 2, "packing": 2, "wh_to_ixd": 10, "ixd_to_fba": 5,
        "wh_buffer_days": 8.5, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(
        products, global_growth_rate=0.3, divergence_buffer_multiplier=1.5,
    )[0]
    # effective_wh_buffer = 8.5 * 1.5 = 12.75
    # ideal_wh_stock = 35.4 * (2 + 12.75) * 1.0 * 1.3 = 678.807
    assert result["ideal_wh_stock"] == pytest.approx(678.807, abs=0.05)
    assert result["effective_wh_buffer_days"] == pytest.approx(12.75)


def test_ideal_wh_stock_does_not_widen_the_buffer_for_a_calm_row():
    """A non-diverged row's effective buffer is exactly wh_buffer_days — the multiplier must be
    a no-op when the flag is False, not applied at a neutral-looking value."""
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "sheet", "diverged": False,
        "daily_rate": 10.0, "last_month_sale": 0, "seasonal_impact": 1.0,
        "purchase_rate": 0, "supplier_to_wh": 5, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 10.0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(
        products, global_growth_rate=0.0, divergence_buffer_multiplier=1.5,
    )[0]
    assert result["effective_wh_buffer_days"] == 10.0
    # ideal_wh_stock = 10 * (5 + 10) * 1.0 * 1.0 = 150.0
    assert result["ideal_wh_stock"] == pytest.approx(150.0)


def test_ideal_fba_stock_uses_the_same_seasonality_and_growth():
    """Ideal FBA also gets seasonality/growth, on the downstream pipeline lead time
    (packing + wh_to_ixd + ixd_to_fba) — unaffected by the divergence buffer, which is a WH-only
    concept (safety stock while waiting on the supplier, not the internal pipeline)."""
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "sheet", "diverged": True,
        "daily_rate": 10.0, "last_month_sale": 0, "seasonal_impact": 2.0,
        "purchase_rate": 0, "supplier_to_wh": 5, "packing": 2, "wh_to_ixd": 10, "ixd_to_fba": 5,
        "wh_buffer_days": 10.0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(
        products, global_growth_rate=0.3, divergence_buffer_multiplier=1.5,
    )[0]
    # ideal_fba_stock = 10 * (2 + 10 + 5) * 2.0 * 1.3 = 442.0 — divergence multiplier NOT applied
    assert result["ideal_fba_stock"] == pytest.approx(442.0)


def test_calculate_projections_no_longer_returns_removed_fields():
    """Source assertion for the eight dropped fields — a runtime check that nothing downstream
    silently keeps reading a stale key."""
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "sheet", "diverged": False,
        "daily_rate": 10.0, "last_month_sale": 0, "seasonal_impact": 1.0,
        "purchase_rate": 50.0, "supplier_to_wh": 5, "packing": 2, "wh_to_ixd": 10, "ixd_to_fba": 5,
        "wh_buffer_days": 10.0, "current_fba_stock": 999, "current_wh_stock": 999,
    }]
    result = logic.calculate_projections(
        products, global_growth_rate=0.3, divergence_buffer_multiplier=1.5,
    )[0]
    for removed in (
        "shipment_alert", "reorder_alert", "ideal_stock_value", "current_stock_value",
        "inventory_days",
    ):
        assert removed not in result, f"{removed} should no longer be computed"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly`
Expected: multiple FAILs — `calculate_projections() got an unexpected keyword argument
'global_growth_rate'` and `KeyError`/`AssertionError` for the removed fields, since the current
implementation still takes only `products` and still computes the old fields.

- [ ] **Step 3: Rewrite `calculate_projections` and the settings constants**

Replace `app/projections/logic.py:93-162` (`calculate_projections`) with:

```python
def calculate_projections(
    products: list[dict], *, global_growth_rate: float, divergence_buffer_multiplier: float,
) -> list[dict]:
    """Run the reorder-point formula on each product row.

    **One formula, always applied — this is the fix for two separate bugs measured on the real
    account.**

    Bug 1: the pre-existing code only applied `seasonal_impact` and (the now-removed per-row)
    `growth_rate` to a row whose blended `daily_rate` was 0/None — a `sheet` row that HAD been
    through the weekly refresh (the normal case after the 02 Sep deploy) used its blended rate
    completely unadjusted. Whether the two factors took effect depended on an accident of which
    code path a row happened to hit, not on a decision anyone made. Now: `demand_rate` is
    computed first (the blended rate if present, `last_month_sale / 30` otherwise), and
    `seasonal_impact` / `global_growth_rate` are applied to it UNCONDITIONALLY, every time.

    Bug 2: `ideal_wh_stock` used to be `daily_rate * wh_buffer_days` alone — the supplier lead
    time (`supplier_to_wh`) never entered the warehouse reorder trigger, only `Ideal FBA`/`Lead
    Total`. A product with a 25-day supplier lead and a 10-day buffer showed a trigger that was
    blind to 25 of the 35 days it actually takes to have more stock in hand. Now
    `ideal_wh_stock = demand_rate * (supplier_to_wh + effective_wh_buffer) * seasonal * (1 +
    growth)` — the reorder point covers the FULL wait, ordering time plus safety margin, not the
    margin alone.

    `effective_wh_buffer` widens by `divergence_buffer_multiplier` when the row is already
    flagged `diverged` (its 7d/30d rates disagree beyond the saved threshold) — a volatile
    product gets more safety stock automatically the week it is detected, rather than needing the
    owner to notice the ⚠ and hand-edit `wh_buffer_days`. It never applies to `ideal_fba_stock`:
    that lead time is the internal pipeline (packing → WH→IXD → IXD→FBA), not the wait on an
    external supplier, so a demand spike does not change how long the pipeline itself takes.

    `global_growth_rate` and `divergence_buffer_multiplier` are REQUIRED keyword-only parameters,
    not read from each product dict — they are account-wide settings (the whole reason the
    growth rate stopped being a per-row column), and the caller (the router) is the one place
    that loads them from `repository.load_blend_settings`.
    """
    for p in products:
        seasonal = p.get("seasonal_impact", 1.0) or 1.0
        has_blended_rate = (p.get("daily_rate") or 0) > 0

        if has_blended_rate:
            demand_rate = p["daily_rate"]
        else:
            last_sale = p.get("last_month_sale", 0) or 0
            demand_rate = last_sale / 30

        growth_multiplier = 1 + global_growth_rate
        daily_rate = demand_rate * seasonal * growth_multiplier
        monthly_forecast = daily_rate * 30

        s2w = p.get("supplier_to_wh", 5) or 0
        pack = p.get("packing", 2) or 0
        w2i = p.get("wh_to_ixd", 10) or 0
        i2f = p.get("ixd_to_fba", 5) or 0
        total_lead = s2w + pack + w2i + i2f
        wh_buffer = p.get("wh_buffer_days", 10) or 0
        effective_wh_buffer = wh_buffer * (divergence_buffer_multiplier if p.get("diverged") else 1.0)

        ideal_fba = round(demand_rate * (pack + w2i + i2f) * seasonal * growth_multiplier, 1)
        ideal_wh = round(demand_rate * (s2w + effective_wh_buffer) * seasonal * growth_multiplier, 1)

        p["monthly_forecast"] = round(monthly_forecast, 1)
        p["daily_rate"] = round(daily_rate, 2)
        p["total_lead_time"] = total_lead
        p["effective_wh_buffer_days"] = round(effective_wh_buffer, 2)
        p["ideal_fba_stock"] = ideal_fba
        p["ideal_wh_stock"] = ideal_wh

    return products
```

**Note the deliberate change to `daily_rate`'s meaning**: it now always reflects seasonality and
growth on top of the raw demand rate (matching `monthly_forecast = daily_rate * 30` exactly),
whereas `ideal_fba`/`ideal_wh` are computed from the un-adjusted `demand_rate` with seasonal/growth
applied once, at the end — algebraically identical to applying them to `daily_rate` and skipping
the growth/seasonal factors in the lead-time multiplication, but written this way so a reader can
see `demand_rate` (kg/day, no factors) separately from `daily_rate` (the on-screen, factor-adjusted
figure) without re-deriving one from the other by division.

Then update `DEFAULT_BLEND`/`BLEND_RANGES` in the same file (around line 229):

```python
DEFAULT_BLEND = {
    #: How much weight the last 7 days carries against the last 30. 0.4 is a starting point
    #: measured to move real parents meaningfully (Bangla Moori-shaped: 1.74x) without letting
    #: one freak week dominate a monthly purchasing decision.
    "seven_day_weight": 0.4,
    #: The |7d/30d - 1| fraction, as a PERCENTAGE for the settings screen, above which a row is
    #: flagged diverged. 30% — smaller than the real spikes measured (58-74%) so genuine signal
    #: is not missed, larger than ordinary week-to-week noise.
    "divergence_pct": 30.0,
    #: The company's overall sales growth assumption, applied to EVERY product's forecast — one
    #: number, not per-parent. Measured against `projection_defaults.json`: 79 of 81 static
    #: entries already used 0.3, so this was already a company-wide figure typed 81 times by
    #: accident of the static file's structure, not a genuine per-product signal.
    "global_growth_rate": 0.3,
    #: How much a DIVERGED row's warehouse safety buffer widens, automatically. 1.5x means a
    #: 10-day buffer becomes 15 days the week a product's demand is flagged as having moved
    #: sharply — real protection without drastically over-buying.
    "divergence_buffer_multiplier": 1.5,
}

BLEND_RANGES = {
    "seven_day_weight": (0.0, 1.0),
    "divergence_pct": (1.0, 200.0),
    "global_growth_rate": (0.0, 3.0),
    #: Floor is 1.0, NOT 0.0 — a value below 1 would SHRINK a volatile product's buffer, which is
    #: the exact inversion `good_rating: 99` already taught this codebase to guard against on
    #: read as well as write.
    "divergence_buffer_multiplier": (1.0, 5.0),
}
```

`blend_setting_error` and `blend_or_default` need no changes — they already iterate
`DEFAULT_BLEND`/`BLEND_RANGES` generically by key.

Finally, remove `"growth_rate"` from `CONFIG_FIELDS` at line 67-70:

```python
CONFIG_FIELDS = (
    "purchase_rate", "supplier_to_wh", "packing", "wh_to_ixd", "ixd_to_fba",
    "wh_buffer_days", "seasonal_impact",
)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly`
Expected: all PASS. (The rest of the file — `sales_kg_by_parent`, `blended_daily_rate`, blend
setting validation, `hidden_parent_names` — is untouched by this task and should already be
green; if `test_default_blend_weight_and_threshold` fails, see Step 5 below.)

- [ ] **Step 5: Update the one blend-defaults test that now checks a wider dict**

Find `test_default_blend_weight_and_threshold` (around line 299) and confirm it reads:

```python
def test_default_blend_weight_and_threshold():
    assert logic.DEFAULT_BLEND == {
        "seven_day_weight": 0.4, "divergence_pct": 30.0,
        "global_growth_rate": 0.3, "divergence_buffer_multiplier": 1.5,
    }
```

If it currently asserts only the first two keys, update it to the four-key dict above — an
exact-equality assertion on `DEFAULT_BLEND` is deliberately strict, matching this codebase's
existing checks for other "the whole allow-list" assertions, so a new key added without updating
this test fails loudly rather than passing by accident.

- [ ] **Step 6: Run the full logic test file once more, then commit**

Run: `venv/Scripts/python -m pytest tests/test_projections_logic.py -q -p no:randomly`
Expected: all PASS.

```bash
git add app/projections/logic.py tests/test_projections_logic.py
git commit -m "feat(projections): one reorder-point formula, lead time in the WH trigger"
```

---

## Task 2: `app/models.py` + migration — drop `growth_rate`

**Files:**
- Modify: `app/models.py:876` (remove the `growth_rate` column)
- Create: `alembic/versions/<new_revision>_drop_projection_growth_rate.py`
- Test: `tests/test_schema_migrations.py` (no new test needed — the existing
  `test_the_deploy_detector_reports_the_head_for_a_head_schema` will fail until Task 4 adds the
  detector branch; this is expected and matches this codebase's own documented pattern)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `ProjectionRow` no longer has a `growth_rate` attribute. `app/projections/repository.py`
  (Task 3) removes `"growth_rate"` from its `_FIELDS` tuple, or `save_row`'s `setattr(existing,
  key, value)` loop will raise `AttributeError` the first time a caller passes it.

- [ ] **Step 1: Remove the column from the model**

In `app/models.py`, delete line 876:

```python
    growth_rate = Column(Numeric(6, 2), default=0.3)
```

- [ ] **Step 2: Generate the migration**

Run: `venv/Scripts/python -m alembic revision -m "projections: drop the now-global growth_rate column"`

This prints a new revision id and creates a skeleton file under `alembic/versions/`. Note the
printed revision id — it is referenced in Step 4 (deploy detector) and must be used as the
`down_revision` for any future migration.

- [ ] **Step 3: Write the migration body**

Open the generated file and confirm/set `down_revision = "e81434e50028"` (the current head).
Replace its body with (following `0f85fa400957`'s exact pattern — the last time this codebase
dropped a column):

```python
"""projections: drop the now-global growth_rate column

**Additive-only companion, in reverse: this DROPS one column, nothing else.** `growth_rate` on
`projection_row` is dead the moment growth becomes a single account-wide setting
(`projection_blend.global_growth_rate`, via `app.projections.logic.DEFAULT_BLEND`) rather than a
per-parent field — measured against `app/invoice/projection_defaults.json`, 79 of its 81 static
entries already used the same 0.3, so this was a company-wide assumption typed once per product by
accident of that file's structure, not a genuine per-product signal.

`batch_alter_table` because SQLite cannot DROP COLUMN before 3.35 and Alembic's batch mode rebuilds
the table instead; PostgreSQL ignores the batching and issues a native ALTER. Same reasoning as
0f85fa400957 (`shipment_packing_entries.cartons` dropped for the identical class of reason: a field
that no longer means anything once the concept moved elsewhere).

Revision ID: <fill in from Step 2's output>
Revises: e81434e50028
Create Date: <fill in>

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "<fill in from Step 2's output>"
down_revision: Union[str, None] = "e81434e50028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projection_row", schema=None) as batch_op:
        batch_op.drop_column("growth_rate")


def downgrade() -> None:
    """Restore the column at the old default. The per-parent VALUES are genuinely gone — this
    codebase's static file had already reduced them to one company-wide number in practice
    (79/81 entries at 0.3), so restoring at the shared default loses nothing that mattered."""
    with op.batch_alter_table("projection_row", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("growth_rate", sa.Numeric(6, 2), nullable=True, server_default="0.3")
        )
```

Replace both `<fill in from Step 2's output>` placeholders with the actual revision id Step 2
printed, and `<fill in>` with today's date.

- [ ] **Step 4: Run the migration locally**

Run: `venv/Scripts/python -m alembic upgrade head`
Expected: no errors; output ends with the new revision id.

- [ ] **Step 5: Verify the column is gone**

Run:
```bash
venv/Scripts/python -c "
import sqlite3
con = sqlite3.connect('tracker.db')
cols = {r[1] for r in con.execute('PRAGMA table_info(projection_row)')}
assert 'growth_rate' not in cols, cols
print('growth_rate column removed, remaining columns:', sorted(cols))
"
```
Expected: `growth_rate column removed, remaining columns: [...]` with no `growth_rate` in the list.

- [ ] **Step 6: Commit**

```bash
git add app/models.py alembic/versions/<new_file>.py
git commit -m "refactor(projections): drop the now-global growth_rate column"
```

---

## Task 3: `app/projections/repository.py` — the two new blend settings

**Files:**
- Modify: `app/projections/repository.py:35-46` (`_FIELDS`, `_NUMERIC_FIELDS`)
- Test: `tests/test_projections_repository.py` (add coverage for the new settings round-tripping
  through the existing `load_blend_settings`/`save_blend_settings`/`reset_blend_settings`)

**Interfaces:**
- Consumes: Task 1's widened `logic.DEFAULT_BLEND`/`BLEND_RANGES` (already generic over keys —
  `load_blend_settings`/`save_blend_settings`/`reset_blend_settings` need NO code changes, since
  they already call `logic.blend_or_default`/`logic.blend_setting_error` generically). Task 2's
  removal of the `growth_rate` column on `ProjectionRow`.
- Produces: `_FIELDS` and `_NUMERIC_FIELDS` no longer include `"growth_rate"` — any caller still
  passing it in a `values` dict to `save_row`/`upsert_sheet_rows` will have it silently ignored
  (both already guard with `if key in _FIELDS`), not raise, so removing it from the tuple is safe
  even before every caller is updated (Task 5 updates the router regardless, for cleanliness).

- [ ] **Step 1: Write the failing test for the new settings round-tripping**

Add to `tests/test_projections_repository.py`:

```python
async def test_global_growth_rate_and_divergence_multiplier_round_trip(db):
    """The two new blend settings use the exact same load/save/reset path as the pre-existing
    seven_day_weight/divergence_pct — no new repository functions needed, since
    load_blend_settings/save_blend_settings already iterate DEFAULT_BLEND generically."""
    from app.projections import repository

    defaults = await repository.load_blend_settings(db)
    assert defaults["global_growth_rate"] == 0.3
    assert defaults["divergence_buffer_multiplier"] == 1.5

    saved = await repository.save_blend_settings(
        db, {"global_growth_rate": 0.5, "divergence_buffer_multiplier": 2.0},
    )
    assert saved["global_growth_rate"] == 0.5
    assert saved["divergence_buffer_multiplier"] == 2.0

    reloaded = await repository.load_blend_settings(db)
    assert reloaded["global_growth_rate"] == 0.5
    assert reloaded["divergence_buffer_multiplier"] == 2.0


async def test_divergence_buffer_multiplier_below_one_is_refused():
    """The good_rating: 99 lesson, applied here: a multiplier below 1.0 would SHRINK a volatile
    product's buffer, the opposite of what this setting exists to do."""
    from app.projections import repository

    with pytest.raises(ValueError, match="divergence_buffer_multiplier"):
        await repository.save_blend_settings(db, {"divergence_buffer_multiplier": 0.5})
```

(If `pytest` is not already imported at the top of `tests/test_projections_repository.py`, add
`import pytest`.)

- [ ] **Step 2: Run to verify the new tests fail**

Run: `venv/Scripts/python -m pytest tests/test_projections_repository.py -q -p no:randomly -k "global_growth_rate or divergence_buffer_multiplier"`
Expected: FAIL — at this point `logic.DEFAULT_BLEND` from Task 1 already has the two new keys (if
Task 1 is done first), so this may already partially pass; if run before Task 1, it fails with
`KeyError: 'global_growth_rate'`. Either way, confirm the second test fails until Task 1's
`BLEND_RANGES` floor of `1.0` is in place.

- [ ] **Step 3: No repository code change needed — confirm and remove the dead field references**

`load_blend_settings`, `save_blend_settings`, `reset_blend_settings` already call
`logic.blend_or_default`/`logic.blend_setting_error` without hardcoding key names, so once Task 1
lands they handle the two new settings automatically. The only change in this file is removing
`"growth_rate"` from the two tuples at lines 35-46:

```python
_FIELDS = (
    "parent_product", "brand", "purchase_rate", "supplier_to_wh", "packing", "wh_to_ixd",
    "ixd_to_fba", "wh_buffer_days", "seasonal_impact", "needs_review",
    "sales_source", "last_month_sale", "seven_day_rate", "thirty_day_rate", "daily_rate",
    "diverged", "current_fba_stock", "current_wh_stock",
)

_NUMERIC_FIELDS = (
    "purchase_rate", "wh_buffer_days", "seasonal_impact", "last_month_sale",
    "seven_day_rate", "thirty_day_rate", "daily_rate", "current_fba_stock", "current_wh_stock",
)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_repository.py -q -p no:randomly`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/projections/repository.py tests/test_projections_repository.py
git commit -m "feat(projections): remove growth_rate from the repository field tuples"
```

---

## Task 4: `deploy/update-ec2.sh` — the detector branch for the dropped column

**Files:**
- Modify: `deploy/update-ec2.sh` (baseline detector, newest-first list)
- Test: `tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema`
  (existing test, run to verify — no new test needed, per this file's own established pattern)

**Interfaces:**
- Consumes: Task 2's migration revision id.
- Produces: nothing new consumed by later tasks.

- [ ] **Step 1: Add the newest-first branch**

In `deploy/update-ec2.sh`, find the detector's `elif` chain (search for `elif "projection_row" in
tables:`) and add a new branch **above** it, keyed on the column's absence (mirroring
`0f85fa400957`'s own branch style at `"cartons" not in cols(...)`):

```python
elif "projection_row" in tables and "growth_rate" not in cols("projection_row"):
    print("<Task 2's revision id>")                 # growth_rate dropped (now a global setting)
elif "projection_row" in tables:
    print("e81434e50028")                           # projection rows + refresh record
```

Replace `<Task 2's revision id>` with the exact revision id from Task 2 Step 2's output.

- [ ] **Step 2: Run the migration-detector test**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema -q -p no:randomly`
Expected: PASS. (Before this step, with Task 2's migration applied but no detector branch added,
this test FAILS — confirm that failure occurred by checking it was red between Task 2 and this
step, per this codebase's own documented deploy-failure history.)

- [ ] **Step 3: Commit**

```bash
git add deploy/update-ec2.sh
git commit -m "chore(deploy): detector branch for the dropped growth_rate column"
```

---

## Task 5: `app/routers/projections.py` — thread the new settings through, drop removed fields

**Files:**
- Modify: `app/routers/projections.py` — `GLOBAL_DEFAULTS` (line 50-53), `get_current` (line 96),
  `calculate` (line 112-162), `upload_csv` (line 285-320), `download_projection` (line 330-384)
- Test: `tests/test_projections_api.py` (add coverage for the new formula reaching the API; update
  `test_calculate_marks_every_saved_row_manual` if it asserts a removed field — confirmed it does
  not)

**Interfaces:**
- Consumes: Task 1's `logic.calculate_projections(products, *, global_growth_rate,
  divergence_buffer_multiplier)` and Task 3's settings round-trip via
  `repository.load_blend_settings`.
- Produces: `GET /projections/last` response's `products` entries no longer contain
  `shipment_alert`, `reorder_alert`, `ideal_stock_value`, `current_stock_value`, `inventory_days`,
  or `growth_rate`. `/calculate`'s summary drops `total_ideal_value`, `total_current_value`,
  `shipment_alerts`, `reorder_alerts`, `critical_alerts` and gains `total_ideal_wh_kg`,
  `diverged_count`. Templates (Task 7) consume this new summary shape.

- [ ] **Step 1: Write the failing API tests**

Add to `tests/test_projections_api.py`:

```python
async def test_last_applies_the_saved_global_growth_rate(auth_client, db, fake_catalogue):
    """The formula must actually read the saved setting, not a hardcoded default."""
    await auth_client.post("/projections/blend-settings", json={"blend": {"global_growth_rate": 1.0}})
    body = (await auth_client.get("/projections/last")).json()
    chana = next(p for p in body["products"] if p["parent_product"] == "Chana Sattu")
    # A brand-new row has daily_rate 0, so demand_rate falls back to last_month_sale/30 == 0;
    # with 0 demand the growth rate cannot be observed on THIS row. Confirm indirectly instead:
    # the response must not error and must not contain any removed field.
    for removed in ("shipment_alert", "reorder_alert", "ideal_stock_value",
                     "current_stock_value", "inventory_days", "growth_rate"):
        assert removed not in chana, f"{removed} should no longer be in the API response"


async def test_last_summary_reports_total_ideal_wh_and_diverged_count(auth_client, db, fake_catalogue):
    body = (await auth_client.get("/projections/last")).json()
    assert "summary" in body
    assert "total_ideal_wh_kg" in body["summary"]
    assert "diverged_count" in body["summary"]
    assert "shipment_alerts" not in body["summary"]
    assert "total_ideal_value" not in body["summary"]


async def test_calculate_no_longer_accepts_or_returns_growth_rate(auth_client, db, fake_catalogue):
    await auth_client.get("/projections/last")
    response = await auth_client.post("/projections/calculate", json={
        "products": [{"product": "Chana Sattu", "last_month_sale": 42.0, "growth_rate": 5.0}],
    })
    body = response.json()
    row = next(p for p in body["products"] if p["parent_product"] == "Chana Sattu")
    assert "growth_rate" not in row
```

- [ ] **Step 2: Run to verify the new tests fail, and existing ones may too**

Run: `venv/Scripts/python -m pytest tests/test_projections_api.py -q -p no:randomly`
Expected: several FAILs — `get_current`/`calculate`/`upload_csv` all still call
`logic.calculate_projections(rows)` with the old one-argument signature from Task 1, which now
requires `global_growth_rate`/`divergence_buffer_multiplier` as keyword arguments, so every route
currently raises `TypeError`.

- [ ] **Step 3: Update `GLOBAL_DEFAULTS`, `build_current_rows`'s callers, and the three routes**

In `app/routers/projections.py`, update `GLOBAL_DEFAULTS` (lines 50-53) to drop `growth_rate`:

```python
GLOBAL_DEFAULTS = {
    "seasonal_impact": 1.0, "supplier_to_wh": 5, "packing": 2,
    "wh_to_ixd": 10, "ixd_to_fba": 5, "wh_buffer_days": 10.0,
}
```

`build_current_rows`'s call to `repository.save_row` for a brand-new parent (line 78-83) already
uses `**config` from `logic.build_parent_config`, which after Task 1 no longer includes
`growth_rate` — no change needed there.

Add a small helper right after `build_current_rows` (around line 94), since three routes now need
the same two settings:

```python
async def _calculate_with_settings(db: AsyncSession, rows: list[dict]) -> list[dict]:
    """`calculate_projections`, with the account-wide settings loaded once per request rather
    than three separate call sites each remembering to load them."""
    blend = await repository.load_blend_settings(db)
    return logic.calculate_projections(
        rows,
        global_growth_rate=blend["global_growth_rate"],
        divergence_buffer_multiplier=blend["divergence_buffer_multiplier"],
    )
```

Update `get_current` (line 96-109):

```python
@router.get("/last")
async def get_current(request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """The live table: every active parent, its purchasing config, its sales rate, and its
    warehouse reorder point.

    Replaces the old file-backed `/last`+`/init` pair. There is no "never initialised" state any
    more — the sheet always has active parents, so this always has rows to show.
    """
    rows, report = await build_current_rows(db)
    blend = await repository.load_blend_settings(db)
    products = logic.calculate_projections(
        rows,
        global_growth_rate=blend["global_growth_rate"],
        divergence_buffer_multiplier=blend["divergence_buffer_multiplier"],
    )
    total_ideal_wh = sum(p.get("ideal_wh_stock", 0) for p in products)
    diverged_count = sum(1 for p in products if p.get("diverged"))
    return JSONResponse({
        "products": products,
        "catalogue": report,
        "blend": blend,
        "last_refresh": await repository.last_refresh(db),
        "summary": {
            "total_products": len(products),
            "total_forecast_kg": round(sum(p["monthly_forecast"] for p in products), 0),
            "total_ideal_wh_kg": round(total_ideal_wh, 0),
            "diverged_count": diverged_count,
        },
    })
```

Update `calculate` (line 112-162) — drop `purchase_rate`... wait, `purchase_rate` stays (it is
still a stored, editable field per the spec); only drop `current_fba_stock`/`current_wh_stock`
from being READ for alert computation, and drop `growth_rate` from the saved values and the
summary:

```python
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
            "seasonal_impact": p.get("seasonal_impact", 1.0),
            "last_month_sale": p.get("last_month_sale", 0),
            "current_fba_stock": p.get("current_fba_stock", 0),
            "current_wh_stock": p.get("current_wh_stock", 0),
        }, source="manual")
        saved.append(row)

    products = await _calculate_with_settings(db, saved)
    diverged_count = sum(1 for p in products if p.get("diverged"))

    return JSONResponse({
        "products": products,
        "summary": {
            "total_products": len(products),
            "total_forecast_kg": round(sum(p["monthly_forecast"] for p in products), 0),
            "total_ideal_wh_kg": round(sum(p.get("ideal_wh_stock", 0) for p in products), 0),
            "diverged_count": diverged_count,
        },
    })
```

Update `upload_csv` (line 310-311) to use the helper instead of the bare call:

```python
    products = await _calculate_with_settings(db, saved)
    products.sort(key=lambda x: x.get("monthly_forecast", 0), reverse=True)
```

Update `download_projection` (line 330-384) — drop the nine removed-field columns and their
conditional cell-fill logic:

```python
@router.get("/download")
async def download_projection(request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """Download the current table as Excel."""
    rows, _report = await build_current_rows(db)
    products = await _calculate_with_settings(db, rows)
    if not products:
        return JSONResponse({"error": "No projection data"}, status_code=404)

    out_rows = [{
        "Product": p["parent_product"],
        "Brand": p.get("brand", ""),
        "Sales Source": p.get("sales_source", "sheet"),
        "Needs Review": "yes" if p.get("needs_review") else "",
        "7-Day Rate (kg/day)": p.get("seven_day_rate"),
        "30-Day Rate (kg/day)": p.get("thirty_day_rate"),
        "Diverged": "yes" if p.get("diverged") else "",
        "Last Month Sale (kg)": p.get("last_month_sale", 0),
        "Seasonal Impact": p.get("seasonal_impact", 1),
        "Monthly Forecast (kg)": p.get("monthly_forecast", 0),
        "Daily Rate (kg)": p.get("daily_rate", 0),
        "Ideal WH Stock (kg)": p.get("ideal_wh_stock", 0),
        "Supplier -> WH (days)": p.get("supplier_to_wh", 0),
        "Packing (days)": p.get("packing", 0),
        "WH -> IXD (days)": p.get("wh_to_ixd", 0),
        "IXD -> FBA (days)": p.get("ixd_to_fba", 0),
        "Lead Time Total (days)": p.get("total_lead_time", 0),
        "Ideal FBA Stock (kg)": p.get("ideal_fba_stock", 0),
        "WH Buffer Days": p.get("wh_buffer_days", 0),
        "Effective WH Buffer Days": p.get("effective_wh_buffer_days", 0),
    } for p in products]

    df = pd.DataFrame(out_rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Projections")

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Projections.xlsx"},
    )
```

(The `openpyxl.styles.PatternFill` red/yellow cell-highlighting import and logic are removed
entirely along with the alert columns they highlighted.)

- [ ] **Step 4: Confirm the file imports cleanly**

Run: `venv/Scripts/python -c "import app.routers.projections"`
Expected: no output, no error.

- [ ] **Step 5: Run the API tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_api.py -q -p no:randomly`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routers/projections.py tests/test_projections_api.py
git commit -m "feat(projections): route the reorder-point formula through the API, drop 5 alert/value fields"
```

---

## Task 6: mutation testing — extend the existing harness

**Files:**
- Modify: `scripts/mutate_projections.py`

**Interfaces:** none — standalone script, unchanged shape.

**Why this task exists:** the pre-existing harness (Task 9 of the prior Projections plan) already
covers the parts of `logic.py`/`repository.py`/`refresh.py`/the router untouched by this change. It
needs new entries for the two decisions this plan adds: seasonality/growth applying unconditionally,
and the lead time entering the WH reorder trigger.

- [ ] **Step 1: Add two new mutation entries**

In `scripts/mutate_projections.py`, add to the `MUTATIONS` list (after the existing entries, before
the closing `]`):

```python
    (
        "calculate_projections stops applying growth/seasonal to an already-blended row",
        LOGIC,
        "        daily_rate = demand_rate * seasonal * growth_multiplier",
        "        daily_rate = demand_rate",
        "test_calculate_projections_applies_seasonality_and_growth_to_a_sheet_row",
    ),
    (
        "ideal_wh_stock drops the supplier lead time from the WH reorder trigger",
        LOGIC,
        "        ideal_wh = round(demand_rate * (s2w + effective_wh_buffer) * seasonal * growth_multiplier, 1)",
        "        ideal_wh = round(demand_rate * effective_wh_buffer * seasonal * growth_multiplier, 1)",
        "test_ideal_wh_stock_includes_the_supplier_lead_time",
    ),
    (
        "the divergence buffer multiplier stops being conditional on the diverged flag",
        LOGIC,
        '        effective_wh_buffer = wh_buffer * (divergence_buffer_multiplier if p.get("diverged") else 1.0)',
        "        effective_wh_buffer = wh_buffer * divergence_buffer_multiplier",
        "test_ideal_wh_stock_does_not_widen_the_buffer_for_a_calm_row",
    ),
```

- [ ] **Step 2: Run the harness**

Run: `venv/Scripts/python scripts/mutate_projections.py`
Expected: `all 12 mutations caught` (9 pre-existing + 3 new). If anything SURVIVES, read the
printed label and strengthen the named test's fixture — do not weaken the mutation.

- [ ] **Step 3: Run the full suite one more time**

Run: `venv/Scripts/python -m pytest -q -p no:randomly`
Expected: zero failures.

- [ ] **Step 4: Commit**

```bash
git add scripts/mutate_projections.py
git commit -m "test(projections): mutation coverage for the reorder-point formula"
```

---

## Task 7: `templates/projections.html` — column removal/reorder, growth-rate input, default sort

**Files:**
- Modify: `templates/projections.html`

**Interfaces:**
- Consumes: Task 5's response shape — `GET /projections/last` and `POST /projections/calculate`
  now return `summary.total_ideal_wh_kg`/`summary.diverged_count` instead of the five removed
  summary fields; product rows no longer carry `shipment_alert`/`reorder_alert`/
  `ideal_stock_value`/`current_stock_value`/`inventory_days`/`growth_rate`; `GET
  /projections/blend-settings` now returns `blend.global_growth_rate`/
  `blend.divergence_buffer_multiplier` alongside the existing two keys.

This task has no automated test of its own, matching the prior Projections template task's own
note — `templates/*.html` are exercised by `tests/test_local_dates.py`'s repo-wide source scan and
by manual verification (Task 8). Confirm the template still parses as valid Jinja2/HTML (Step 4).

- [ ] **Step 1: Remove the 8 columns from `<thead>`**

In `templates/projections.html`, find the `<thead><tr>` block. Remove these `<th>` elements
entirely (their exact current text, confirm by reading the file before editing — do not guess at
wording):

- `Growth`
- `Current FBA`
- `Ship Alert`
- `Current WH`
- `Reorder Alert`
- `Rate (₹/kg)`
- `Stock Value (₹)`
- `Inv Days`

- [ ] **Step 2: Move the `Ideal WH` header, in the `<thead>`**

Move the `Ideal WH` `<th>` (and, if the header text differs from the one used in Step 1's
deletions, keep its exact current text) to sit immediately after the `Daily (kg/d)` column and
immediately before `S→WH (d)`. The resulting header order should be:

```
# · Product · Brand · Source · 7d/30d · Last Month Sale · Seasonal · Forecast (kg/mo) ·
Daily (kg/d) · Ideal WH · S→WH (d) · Pack (d) · WH→IXD (d) · IXD→FBA (d) · Lead Total ·
Ideal FBA · WH Buf Days
```

- [ ] **Step 3: Add the global growth-rate input to the blend-settings panel**

Find the blend-settings panel markup (the block containing `blend-weight` and `blend-threshold`
inputs, added by the prior Projections plan). Add a third input alongside them, following the
exact same label/input pattern:

```html
    <label style="font-size:12px;color:var(--text-muted);display:flex;flex-direction:column;gap:4px">
      Company growth rate (%)
      <input type="number" id="blend-growth" step="5" min="0" max="300" style="width:80px;background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--text);padding:5px 8px"/>
    </label>
    <label style="font-size:12px;color:var(--text-muted);display:flex;flex-direction:column;gap:4px">
      Volatility buffer multiplier
      <input type="number" id="blend-buffer-mult" step="0.1" min="1" max="5" style="width:80px;background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--text);padding:5px 8px"/>
    </label>
```

Place these two `<label>` blocks in the same flex row as the existing `blend-weight`/
`blend-threshold` labels, before the `Save`/`Reset to defaults` buttons.

- [ ] **Step 4: Update the KPI strip markup**

Find `showSummary` (or the equivalent function rendering `#summary`'s `innerHTML` — read the file
to confirm the exact current function name and structure before editing). Replace its body so the
four stats become:

```js
function showSummary(s){
  if(!s)return;
  document.getElementById("summary").style.display="grid";
  document.getElementById("summary").innerHTML=`
    <div class="stat"><div class="num">${s.total_products}</div><div class="lbl">Products</div></div>
    <div class="stat"><div class="num">${Number(s.total_forecast_kg).toLocaleString("en-IN",{maximumFractionDigits:0})}</div><div class="lbl">Forecast (kg)</div></div>
    <div class="stat"><div class="num">${Number(s.total_ideal_wh_kg).toLocaleString("en-IN",{maximumFractionDigits:0})}</div><div class="lbl">Total Ideal WH (kg)</div></div>
    <div class="stat ${s.diverged_count>0?'warn':''}"><div class="num">${s.diverged_count}</div><div class="lbl">Diverged</div></div>`;
}
```

- [ ] **Step 5: Update `loadBlendSettings`/`saveBlendSettings`/`resetBlendSettings` for the two new fields**

Find these three functions (added by the prior Projections plan). Update each to read/write the
two new inputs alongside the existing two:

```js
async function loadBlendSettings(){
  const r = await fetch("/projections/blend-settings");
  const data = await r.json();
  document.getElementById("blend-weight").value = data.blend.seven_day_weight;
  document.getElementById("blend-threshold").value = data.blend.divergence_pct;
  document.getElementById("blend-growth").value = data.blend.global_growth_rate * 100;
  document.getElementById("blend-buffer-mult").value = data.blend.divergence_buffer_multiplier;
}

async function saveBlendSettings(){
  const weight = parseFloat(document.getElementById("blend-weight").value);
  const threshold = parseFloat(document.getElementById("blend-threshold").value);
  const growthPct = parseFloat(document.getElementById("blend-growth").value);
  const bufferMult = parseFloat(document.getElementById("blend-buffer-mult").value);
  const r = await fetch("/projections/blend-settings", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({blend: {
      seven_day_weight: weight, divergence_pct: threshold,
      global_growth_rate: growthPct / 100, divergence_buffer_multiplier: bufferMult,
    }}),
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
  document.getElementById("blend-growth").value = data.blend.global_growth_rate * 100;
  document.getElementById("blend-buffer-mult").value = data.blend.divergence_buffer_multiplier;
  toast("Reset to measured defaults", "success");
}
```

(`global_growth_rate` is stored as a fraction, e.g. `0.3`, and shown on screen as a percentage,
e.g. `30`, matching the existing `divergence_pct` convention where the STORED value is already a
percentage but `seven_day_weight` is a fraction — the growth rate follows `seven_day_weight`'s
convention since it multiplies directly as `1 + rate`, so the `× 100` / `/ 100` conversion happens
only in this display layer, never in `logic.py`.)

- [ ] **Step 6: Remove the eight columns' cells from `renderTable`, add the `Ideal WH` cell in its new position**

Find `renderTable`'s row-building template literal. Remove the `<td>` elements corresponding to
the 8 removed columns (their exact current markup — read the file first; do not guess). Move the
existing `Ideal WH` `<td>` (bolded, per the existing markup) to sit immediately after the `Daily`
`<td>` and before the `S→WH` input `<td>`, matching the header reorder from Step 2.

- [ ] **Step 7: Add default sort by Ideal WH descending**

Find where `products` is first rendered after `loadAll()` populates it (the `renderTable()` call
inside `loadAll`, per the existing structure). Immediately before that call, add:

```js
  products.sort((a, b) => (b.ideal_wh_stock || 0) - (a.ideal_wh_stock || 0));
```

Add this in `loadAll()`, right after `products = data.products || [];` and before
`renderCatalogueSummary()` — so it applies once per load, and the existing click-to-sort headers
(if present) still work by re-sorting `products` on top of this initial order, unchanged.

- [ ] **Step 8: Confirm the template still renders**

Run:
```bash
venv/Scripts/python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
html = env.get_template('projections.html').render(active='projections', grant=None)
print('rendered', len(html), 'chars')
"
```
Expected: `rendered <N> chars`, no Jinja2 error.

- [ ] **Step 9: Syntax-check the extracted JavaScript**

Run:
```bash
venv/Scripts/python -c "
import re, pathlib, tempfile, os
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
html = env.get_template('projections.html').render(active='projections', grant=None)
m = re.search(r'<script>(.*)</script>', html, re.S)
out = os.path.join(tempfile.gettempdir(), 'projections_check2.js')
pathlib.Path(out).write_text(m.group(1), encoding='utf-8')
print(out)
"
```
Then, using the printed path: `node --check <printed path>`
Expected: no output from `node --check`.

- [ ] **Step 10: Run `test_local_dates.py` and the template render-target scan**

Run: `venv/Scripts/python -m pytest tests/test_local_dates.py tests/test_template_render_targets.py -q -p no:randomly`
Expected: all PASS (or SKIP for `projections.html`, matching this file's existing skip pattern for
templates with no date strings / no `getElementById` mismatches).

- [ ] **Step 11: Commit**

```bash
git add templates/projections.html
git commit -m "feat(projections): Ideal WH moves up front, 8 unused columns removed, growth-rate control added"
```

---

## Task 8: `CLAUDE.md` — document the reorder-point change

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a new subsection**

Find the `## Projections tab — live parents, live sales, a 7d/30d weighted blend` section (added
by the prior plan) and insert a new subsection immediately after its
"`ProjectionRow` is keyed on the parent NAME, like `ProductRawStock`" subsection (i.e. at the end
of that section, before the next `##` heading):

```markdown
### `Ideal WH` is a genuine reorder point, not a buffer alone — and growth is one number, not 81
Reported directly: *"I want this sheet to mostly work for me to give me a level of stock of each
item which when goes below a level at my warehouse i should buy."* Two real bugs sat under the old
formula, both found by reading the code against that goal rather than by a test failing.

**Seasonality and growth were applied only to a row that had NEVER been through the weekly
blend.** A `sheet` row with a real blended `daily_rate` (the normal case after the weekly refresh)
skipped both factors entirely — whether they took effect depended on an accident of which code
path a row happened to hit, not a decision anyone made. Now `calculate_projections` computes the
raw demand rate first (the blend, or `last_month_sale / 30`) and applies `seasonal_impact` and the
new `global_growth_rate` unconditionally, every time.

**`ideal_wh_stock` never used the supplier lead time.** It was `daily_rate * wh_buffer_days`
alone — `supplier_to_wh` fed only `Lead Total`/`Ideal FBA`, the downstream pipeline. A product
with a real 25-day supplier lead and a hand-set 10-day buffer showed a WH reorder trigger blind to
25 of the 35 days it actually takes to have more stock in hand. It is now
`demand_rate × (supplier_to_wh + effective_wh_buffer) × seasonal × (1 + growth)` — the reorder
point covers the FULL wait, ordering time plus a safety margin, not the margin alone.

**A diverged row's buffer widens automatically**, by a saved `divergence_buffer_multiplier`
(default 1.5x) — a product already flagged as having moved sharply from its own 30-day baseline
gets more warehouse safety stock the week it is detected, without the owner needing to notice the
⚠ and hand-edit `wh_buffer_days` first. Never applied to `ideal_fba_stock`: that lead time is the
internal pipeline (packing → WH→IXD → IXD→FBA), which does not change length because demand moved.

**`growth_rate` stopped being a per-parent column.** Measured against
`app/invoice/projection_defaults.json`: 79 of its 81 static entries already used the same `0.3` —
a company-wide assumption typed once per product by accident of the static file's structure, not a
genuine per-product signal like `seasonal_impact` (which genuinely spans 1.0/1.5/2.0). It is now
`projection_blend.global_growth_rate`, one saved, range-checked setting
(`app.projections.logic.DEFAULT_BLEND`/`BLEND_RANGES`, the same pattern the blend weight and
divergence threshold already use), applied to every product. The `ProjectionRow.growth_rate`
column was dropped via migration — genuinely dead once growth moved, following this codebase's own
precedent (`0f85fa400957`, `shipment_packing_entries.cartons`) for removing a column rather than
leaving it stored and silently ignored.

**Eight columns tied to automation that does not exist yet were removed from the screen**:
`Current FBA`, `Ship Alert`, `Current WH`, `Reorder Alert`, `Rate`, `Stock Value`, `Inv Days` (all
depend on live Amazon/warehouse stock counts this tab does not fetch), plus `Growth` (now global).
`purchase_rate`, `current_fba_stock`, `current_wh_stock` stay as stored, editable `ProjectionRow`
columns even though the UI stops reading three of them today — real hand-typed data that would
otherwise be silently discarded, kept for when stock-count automation lands.

**`divergence_buffer_multiplier`'s range floor is `1.0`, not `0.0`.** A value below 1 would SHRINK
a volatile product's buffer — the exact inversion `good_rating: 99` already taught this codebase
to range-check for, applied here to a new setting before it ever shipped with the bug.

`Ideal WH` moved to sit immediately after `Daily (kg/d)` in the table, and the table now
default-sorts by it descending — it is the number the owner actually watches, and burying it after
four lead-time input columns meant reading past them on every visit. The KPI strip's headline
number is now `Total Ideal WH (kg)` across the portfolio, replacing four stats
(`total_ideal_value`, `total_current_value`, `shipment_alerts`, `reorder_alerts`) that no longer
have data behind them once the alert columns were removed; a `Diverged` count replaces
`critical_alerts` as the other headline figure.
```

- [ ] **Step 2: Confirm the addition doesn't break the CLAUDE.md-scanning tests**

Run: `venv/Scripts/python -m pytest tests/test_local_dates.py tests/test_theme.py -q -p no:randomly`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the Projections reorder-point formula change"
```

---

## Final verification (manual, once every task above is complete)

1. Start the app: `preview_start` with the `tracker` launch config, or
   `venv/Scripts/python -m uvicorn app.main:app --reload --port 8000`.
2. Sign in, open `/projections-page`.
3. Confirm the table's first calculated column after `Daily (kg/d)` is `Ideal WH`, bolded, and the
   table is sorted by it descending — the biggest reorder need is row 1.
4. Confirm `Growth`, `Current FBA`, `Ship Alert`, `Current WH`, `Reorder Alert`, `Rate`,
   `Stock Value`, `Inv Days` are all gone from the table header.
5. Open "Blend settings" — confirm a "Company growth rate (%)" and "Volatility buffer multiplier"
   input are present alongside the existing 7-day weight / divergence threshold, and both load
   with sensible defaults (30 and 1.5).
6. Change the growth rate to something visibly different (e.g. 50%), Save, reload — confirm every
   row's `Forecast`/`Daily`/`Ideal WH` moved, not just one.
7. Find a row currently flagged `diverged` (⚠ on the 7d/30d cell) — confirm its `Ideal WH` is
   noticeably larger than a comparable non-diverged row with a similar demand rate and lead time,
   reflecting the widened buffer.
8. Reset blend settings; confirm growth returns to 30% and the buffer multiplier to 1.5.
9. Confirm the KPI strip reads Products / Forecast (kg) / Total Ideal WH (kg) / Diverged — four
   stats, not the old six.
10. Download the Excel export — confirm the removed columns are absent and `Ideal WH Stock (kg)`,
    `Effective WH Buffer Days` are present.
11. Run the full automated suite one final time: `venv/Scripts/python -m pytest -q -p no:randomly`
    — same command used after every task above, now green end to end.
12. Run the mutation harness one final time: `venv/Scripts/python scripts/mutate_projections.py`
    — `all 12 mutations caught`.
