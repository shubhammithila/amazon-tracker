# Projections tab: active-parent filtering, live sales, and a 7d/30d weighted blend

## The requests

1. *"Come to the projections app. Like we did the filtering of active and inactive items on
   shipments tab. same we have to do it in projections app as well. the active parents to be
   shown."*
2. *"I was thinking of doing an api integration here as well and get the live data of last
   month's sales."*
3. *"Basis last 7 days sale I want to update the data weekly on some kind of weighted average
   of last month and last 7 days... to account for [products that] spike or suddenly drop."*

Plus, mid-brainstorm: *"triphala sattu should be shown, everywhere"* — the specific product that
exposed why this needs to be a source change, not a filter.

## What is there today

`app/routers/projections.py` builds every row from **`app/invoice/projection_defaults.json`** —
81 hand-maintained parent products, each with a purchase rate, four lead-time fields and a
seasonal/growth factor. Last month's sales arrive by a **manual Business Report CSV upload**,
mapped ASIN → parent via **`app/invoice/product_families.json`** (205 ASINs, static).

This is the exact design the shipment tab moved away from, for a reason CLAUDE.md records:

> *"Triphala Sattu was in the sheet, marked Active, in two pack sizes — and could never reach a
> plan, because `generate` iterated `product_families.json` ... and used the sheet only for a
> yes/no flag."*

Projections has the same defect today. There are no tests for this router at all.

## Measured, before designing anything

**The MRP sheet already has everything needed.** `app/shipment/catalogue.load_catalogue()`
returns `{asin: {name, weight, brand, active}}` with the fallback chain (sheet → cached copy →
static file) the shipment tab already relies on. Called directly:

```
active ASINs:                108
distinct active product names: 38   (this is the parent-grouping unit — see below)
```

**Filtering to active parents is not a small change to the existing 81** — it's a change of
*source*. Of the 81 static parents, only **37** have any active ASIN reachable through
`product_families.json`; the other 44 split into two very different groups:

| | Count | |
|---|---|---|
| In `product_families.json`, every ASIN inactive | 26 | Genuinely discontinued |
| **Never in `product_families.json` at all** | **18** | The static file has no opinion — includes Bengali Posta, Kasundi, Matar Dal, Khoi Lahi |

And in the other direction: **Triphala Sattu is active in the sheet, 2 ASINs, and is not in
`product_families.json` at all** — so no amount of filtering the existing 81 fixes it. It has to
stop being invisible at the source, not get added back as a special case.

**The sheet's own product `name` is the parent-grouping unit, unmerged.** `product_families.json`
keeps flavour variants (Cheese & Cream Chana, Nimbu Pudina Chana, Peri Peri Chana...) as separate
parents, not folded under "Roasted Chana" — different flavours are different recipes and different
purchase decisions, confirmed by reading the file. Portfolio's `family_label()` merges flavours for
a *display rollup*; that is the wrong tool for a *purchasing* grouping and is not used here.

Matching the sheet's 38 active names against `projection_defaults.json` by normalized name:

```
matched:    24
unmatched:  14 — Bengali Chana Sattu, Bengali Chatpata Roasted Chana, Bengali Gobindobhog Rice,
                 Bengali Roasted Chana, Chana Sattu, Chatpata Masala Roasted Chana,
                 Cheese & Cream Roasted Chana, Hing Jeera Roasted Chana, Jeera Chana Sattu,
                 Makkai Sattu, Nimbu Pudina Roasted Chana, Peri Peri Roasted Chana,
                 Raw Flaxseed, Triphala Sattu
```

("Bengali Gobindobhog Rice" is probably the existing "Govind Bhog Rice" under a spelling variant.
No fuzzy match is attempted — it lands in the reviewable bucket like the other 13 and gets merged
by hand once, same as any other row is edited today.)

**Live sales are already in the database — no new integration.** `economics_snapshot` holds
`units_ordered`, `units_refunded` and `net_units` per child ASIN, refreshed nightly at 07:30 IST.
A 30-day window (`2026-08-02..2026-08-31`) holds 267 ASIN rows; a 7-day window
(`2026-08-21..2026-08-27`) is **already stored**, 267 rows. Both halves of the blend come from a
table that exists, at no new Amazon cost.

**`net_units` goes negative and must not be used.** Measured on the 7-day window: 2 ASINs with
`net_units < 0` (a refund-heavy week). `units_ordered` is the demand signal; refunds are a returns
problem, not lower demand, and a negative daily rate produces a negative purchase quantity.

**The blend is sound in aggregate, and genuinely responsive per product.** At the parent level:

```
30d run-rate: 461.1 kg/day
 7d run-rate: 421.1 kg/day     (0.91x — the two windows broadly agree)
```

but individual parents diverge sharply: Bangla Moori 1.74x, Flax Seed 1.58x (spiking); Miniket
Rice 0.37x, Bangla Roasted Chana 0.43x (dropping). This is exactly the signal requested.

**A zero-sales week is not zero demand.** 4 of 47 currently-selling parents had 30-day sales but
**zero** in the 7-day window (Coconut Thekua, Peanut Thekua, Bangla Chana Sattu, Chawli bori) —
slow movers, not dead ones. Blending a hard zero at 40% weight would cut those forecasts 40% on no
evidence.

## Decisions taken (yours)

- **Show only active parents.** Hide all 44 non-active (not "flag the unknown 18 separately").
- **Derive parents from the sheet's own product name**, not `product_families.json` — this is
  what makes Triphala Sattu (and any future sheet addition) show up automatically, everywhere,
  with no second file to keep in sync.
- **A parent with no matching config gets Global Defaults, flagged "needs review"** — same visual
  language the screen already uses for rows running on globals, not a new state.
- **7-day source: `economics_snapshot`**, the already-live data, not a new fetch or a new API.
- **Blend, and flag the divergence** rather than blending silently or clamping it.

## The rules this must not break

1. **A live product is never invisible because a static file has not heard of it.** This is the
   whole reason for the change — repeating the shipment-tab bug here would be doing the work twice
   to reproduce the same defect.
2. **A hidden row is reported, not silently dropped.** Same rule the shipment tab follows for its
   catalogue diff: excluded and named, capped, never a bare count.
3. **A manual override survives a refresh.** The CSV upload and hand-typed edits are not simply
   overwritten the next time live data or the weekly blend runs.
4. **Nothing here writes to Amazon or auto-executes a purchase.** This tab produces a number a
   human orders against; it does not place an order.

---

## 1. `app/routers/projections.py` — parents come from the sheet, config from defaults

Replace the `for name, d in DEFAULTS.items()` loop (the source of every row) with:

```python
async def build_parent_rows() -> tuple[list[dict], dict]:
    """One row per active parent product, matched to its saved config by name.

    Returns (rows, catalogue_report) — the report names what the sheet excluded, mirroring
    `app/shipment/catalogue.py`'s `catalogue` block on the Shipment tab.
    """
```

- Calls `catalogue.load_catalogue()` — same fallback chain, same on-screen source reporting
  (`sheet` / `cache` / `static file`) already proven on the Shipment tab.
- Groups **active** ASINs by their sheet `name` (unmerged — see measurement above). Each group's
  weight is read from the sheet per ASIN, same as the Shipment tab's per-pack-size weight.
- For each parent name, look up `DEFAULTS` by normalized name (case/space/hyphen-insensitive,
  reusing the same normalization proven above). A match copies its saved fields; no match applies
  the current Global Defaults and sets `needs_review: true`.
- Every row carries `source: "sheet" | "manual"` (see §3) so the screen can tell "this parent's
  sales came from Amazon" from "someone typed a number."

**The hidden 44 are reported, not dropped.** `catalogue_report` carries
`{hidden_count, hidden_names[:8]}`. This borrows the Shipment tab's *convention* — cap the named
list at 8, never show a bare count — not its specific before/after-plan diff, which compares two
plan generations and doesn't apply here; Projections has no "previous load" to diff against, only
the full static 81 versus the live-derived set.

## 2. `app/portfolio/economics.py` / `app/portfolio/repository.py` — reused, not duplicated

**No new Amazon integration.** `economics.fetch_economics()` and `repository.save_snapshot()`
already exist and already run nightly; this tab reads what they store, exactly as the Portfolio
tab does. New in `app/projections/logic.py` (new module, kept pure — no DB, no network, matching
every other feature's `logic.py`):

```python
def sales_kg_by_parent(snapshot_rows, catalogue) -> dict[str, float]:
    """units_ordered x pack weight, summed per parent NAME (not `net_units` — see below)."""

def blended_daily_rate(kg_30d, kg_7d, weight) -> tuple[float, bool]:
    """(rate, diverged). `kg_7d is None` (never `0.0`) means no 7-day snapshot exists for this
    parent yet — falls back to the 30-day rate entirely, distinct from a genuine zero-sales week,
    which IS blended (measured: a real zero-week parent still gets the 40% pull, correctly,
    because the data exists and says zero; only a MISSING window falls back)."""
```

**`units_ordered`, never `net_units`.** `net_units` went negative on 2 ASINs in the measured
7-day window; `units_ordered` cannot, and refunds are excluded deliberately — a returns problem is
not a demand signal.

**Divergence flag:** `diverged = True` when `|7d_rate/30d_rate - 1| > threshold` (default 30%,
a saved setting alongside the blend weight — see §4). The row carries both rates so the screen can
show *why* a number moved, following the same principle as the Ads tab's stale-bid display: a
number that changed with an invisible cause is what erodes trust in the whole screen.

## 3. Manual overrides survive a refresh

`last_month_sale` (and any other cell) becomes editable exactly as today, but the stored row keeps
`source: "sheet" | "manual"`. A weekly or nightly recompute only overwrites `source: "sheet"`
rows; a `manual` row is left alone until the owner explicitly resets it (a small "reset to live
data" control per row, mirroring the Portfolio tab's per-decision clear). This is the same shape as
`product_decision` — the owner's edit is a fact that outlives the next automated pass, not
something a refresh silently reverts.

## 4. Settings: blend weight and divergence threshold

Stored in the shared, name-keyed `portfolio_settings` table under a new name
(`projection_blend`), one JSON row. `PortfolioSettings.name == "thresholds"` is the Portfolio
verdicts' own row and `app/ads/repository.py`'s guardrails use the same table under
`GUARDRAIL_SETTING_NAME` — this follows that pattern with its own load/save pair rather than
reusing either feature's specific functions, since both are hardcoded to their own name. **Range-
checked on read AND write**, the lesson from `good_rating: 99` silently breaking every verdict on
this account. Defaults: `{"seven_day_weight": 0.4, "divergence_pct": 30}`. A Reset restores these.

## 5. The weekly refresh

A new scheduled job, alongside the existing ads/portfolio ones and stated the same way:

```python
PROJECTIONS_REFRESH_IST = (7, 0)   # before portfolio (07:30) and ads (08:00), same reporting API
```

registered through `ist.utc_hhmm(...)` — no bare hour reaches `CronTrigger`, which is the exact
mistake that put the ads/portfolio jobs at 08:50/09:20 IST for months. Runs once a week (not
nightly — the owner asked for weekly, and a 30-day rolling average moves little day to day, so a
tighter cadence buys nothing). Fetches the current 30-day and 7-day economics windows if not
already stored, recomputes every `source: "sheet"` row's blended rate, and saves.

**A failed or partial fetch must not overwrite good data.** Same discipline as the Ads refresh's
`ads_refresh` table: the job records `{status, rows_updated, error}` to a small log (reusing
`economics_refresh`'s shape, or a sibling table if the Portfolio tab's semantics don't fit
cleanly — decided during implementation planning), and the screen can say when figures were last
updated and whether the last attempt failed, rather than silently serving stale numbers with no
indication.

## 6. `templates/projections.html`

- **New column: `Sales source`** — `Sheet (30d/7d blend)` / `Sheet (30d only, no 7d data)` /
  `Manual`, with the two blended rates shown on hover when `diverged`.
- **"Needs review" band** on any row using Global Defaults with no saved config, following the
  same visual language as the existing "running on globals" state — not a new banner style.
- **Hidden-parents note**, same shape as the Shipment tab's catalogue diff: *"44 parents hidden —
  not active in the MRP sheet"* with up to 8 names, expandable.
- **Blend-weight and divergence-threshold controls** in a settings panel, following the Ads tab's
  guardrails-panel pattern (open/close, Save, Reset to measured defaults).

## Files

New: `app/projections/logic.py` · `tests/test_projections_logic.py` ·
`tests/test_projections_api.py` (none exist today for this router)

Changed: `app/routers/projections.py` · `app/scheduler.py` · `templates/projections.html` ·
`app/portfolio/repository.py` (adds `load_settings`/`save_settings` calls for the
`projection_blend` name in the shared `portfolio_settings` table — that table is already
name-keyed and shared across features, not owned by Ads or Portfolio specifically) · `CLAUDE.md`

## Verification

**Automated**

- A parent active in the sheet but absent from `product_families.json` (Triphala-shaped) appears
  in the output, with `needs_review: true` and Global Defaults values.
- A parent in `product_families.json` with every ASIN inactive is absent from the output and named
  in `catalogue_report.hidden_names`.
- `sales_kg_by_parent` sums `units_ordered`, never `net_units`; a fixture with a negative
  `net_units` row must not produce a negative rate.
- `blended_daily_rate`: a real zero-sales 7-day window blends (pulls toward the 40% weight); a
  MISSING 7-day snapshot falls back to the 30-day rate entirely — two different inputs, asserted
  as two different outcomes, so a mutation collapsing "no data" into "zero" is caught.
- `diverged` fires above the threshold and not below it; the stored rates are both readable on
  the row.
- A `source: "manual"` row survives a recompute unchanged; a `source: "sheet"` row updates.
- `projection_blend` settings are range-checked on read and on write (mirrors the existing
  `good_rating: 99` guard test on the Portfolio tab).
- The new scheduler job registers at 07:00 IST via `ist.utc_hhmm`, asserted on the IST value —
  the Ads-tab lesson about asserting the UTC value instead pinning the very bug it was fixing.
- A failed weekly refresh leaves the previous good figures in place and is visible on screen as
  failed, not silently absent.

**Manual, once implemented**

Open Projections and confirm Triphala Sattu appears with a "needs review" band. Confirm the count
shown (37 active parents, ~44 hidden, named) matches a fresh `load_catalogue()` call. Edit one
row's `last_month_sale` by hand, trigger a recompute, and confirm that row is untouched while
others update. Check a spiking parent (Bangla Moori-shaped) shows the divergence flag with both
rates. Confirm the settings panel round-trips the blend weight and threshold, and that an
out-of-range value is refused with a stated reason.

## Out of scope

Auto-generating a purchase order or writing anything to Amazon — this tab produces a number a
human orders against. A general parent-product editing screen in the Products tab (considered and
rejected during brainstorming: it would create a second source of ASIN→parent truth alongside the
sheet, the exact drift this change removes). Fuzzy-matching "Bengali Gobindobhog Rice" to
"Govind Bhog Rice" automatically — it is one of the 14 reviewable rows instead. A daily (rather
than weekly) refresh cadence.
