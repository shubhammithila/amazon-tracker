# Projections tab: a real reorder point for the warehouse, not two disagreeing formulas

## The request

> *"I want this sheet to mostly work for me to give me a level of stock of each item which when
> goes below a level at my warehouse i should buy. I.e. the ideal WH stock which is the 1st thing
> which I want to see. So first I want you to brainstorm on the logic which we can set to get this
> stock level basis the lead time, sales in last 7 and 30 days, seasonality factor, overall company
> growth. everything needs to be taken into account."*

Plus: the growth rate is one number for the whole company, not per-product, so it should not be a
per-row column; and eight columns tied to Amazon-stock automation that does not exist yet
(`Current FBA`, `Ship Alert`, `Current WH`, `Reorder Alert`, `Rate`, `Stock Value`, `Inv Days`, and
`Growth`) should come off the screen until that automation is real.

## What is there today, and why it is two disagreeing formulas rather than one

`app.projections.logic.calculate_projections` branches on `sales_source`:

```python
has_blended_rate = p.get("sales_source") == "sheet" and (p.get("daily_rate") or 0) > 0
if has_blended_rate:
    daily_rate = p["daily_rate"]                      # the 7d/30d blend, AS-IS
    monthly_forecast = daily_rate * 30
else:
    last_sale = p.get("last_month_sale", 0) or 0
    monthly_forecast = last_sale * seasonal * (1 + growth)   # seasonality/growth APPLIED
    daily_rate = monthly_forecast / 30
```

For a `sheet` row that has ever been refreshed (the normal case, per the 02 Sep deploy), seasonal
and growth are never applied to the daily rate — they were only ever applied to the fallback path.
Whether the two adjustment factors take effect at all today depends on an implementation detail
(has this parent's row been through a weekly refresh yet), not on a decision anyone made. That is
the exact bug: not "we should account for seasonality and growth", but that the code currently
decides whether to, silently, per row, based on which code path it happens to hit.

**The `Ideal WH` formula has a second, independent problem**: it does not use the supplier lead
time at all.

```python
ideal_wh = round(daily_rate * wh_buffer, 1)      # wh_buffer_days ONLY
```

`supplier_to_wh` (the time from placing a purchase order to material arriving) only feeds
`Lead Total → Ideal FBA`. So a product with a 25-day supplier lead time and a `wh_buffer_days` of
10 shows a WH reorder trigger that ignores 25 of the 35 days it actually takes to have more stock
in hand. Read literally, the column claims "you're fine" until you are roughly two weeks into a
stockout with a purchase order not yet even placed.

**`growth_rate` is stored per parent** in `projection_defaults.json`/`ProjectionRow.growth_rate`,
and measured across the 81 static entries it takes exactly two values: `0.3` (79 products) and
`0.5` (2 products) — effectively one company-wide assumption typed 81 times, not a genuine
per-product signal like `seasonal_impact` (which spans 1.0/1.5/2.0 and does track real seasonal
products, e.g. sattu at 1.5). Confirms the request: growth belongs above the table, once.

## The formula

One computation, applied identically to every row regardless of `sales_source`:

```
demand_rate        = the blended 7d/30d daily rate (kg/day) — or last_month_sale / 30 for a
                      manual row / a sheet row never yet refreshed
effective_wh_buffer = wh_buffer_days × (divergence_buffer_multiplier if row.diverged else 1.0)
ideal_wh_stock      = demand_rate × (supplier_to_wh + effective_wh_buffer)
                        × seasonal_impact × (1 + global_growth_rate)
ideal_fba_stock     = demand_rate × (packing + wh_to_ixd + ixd_to_fba)
                        × seasonal_impact × (1 + global_growth_rate)
```

**Worked example**, using Govind Bhog Rice's real stored config (`supplier_to_wh=2`,
`wh_buffer_days=8.5`, `seasonal_impact=1.0`) and its real measured rates from the 02 Sep production
refresh (7d=42.3, 30d=30.8 kg/day, blended at the default 0.4 weight = 35.4 kg/day, flagged
`diverged` because `|42.3/30.8 - 1| = 37.3%` exceeds the 30% threshold), at `global_growth_rate=0.3`
and `divergence_buffer_multiplier=1.5`:

```
effective_wh_buffer = 8.5 × 1.5 = 12.75          (widened — this row is diverged)
ideal_wh_stock      = 35.4 × (2 + 12.75) × 1.0 × 1.3 = 678.8 kg
```

Against today's number for the same row — `35.4 × 8.5 = 300.9 kg`, no lead time, no growth — the
new figure is more than double. That is not the formula overshooting; it is the current one never
having accounted for the 2-day supplier lead, the 30% growth assumption, or the fact that this
specific product's demand just moved sharply enough to be flagged.

### Why lead time and buffer are additive, not the buffer alone

The reorder point has to cover the *entire* time between "stock crosses the trigger" and "more
stock is in hand": ordering (`supplier_to_wh`) plus a safety margin for demand variability
(`wh_buffer_days`) during that wait. A buffer alone answers "how much extra do I want on top of
zero lead time", which is not the question being asked.

### Why the divergence multiplier, not a bigger buffer typed by hand

A `diverged` row is *already* the signal that this product's recent demand has moved further than
usual from its own 30-day baseline — that is what the flag means (see the existing Ads/Portfolio
principle: "a forecast number that moved with no visible cause is what erodes trust"). Widening
the buffer automatically for exactly those rows means a real spike gets more safety stock the week
it is detected, without the owner having to notice the ⚠ and hand-edit `wh_buffer_days` before it
matters. The multiplier is a saved, editable, range-checked setting (like the blend weight already
is) — never silent, always visible as a small note on the buffer figure it changed
(`8.5 × 1.5 = 12.75 (volatile)`).

### Why growth is global and seasonality stays per-row

Confirmed by the owner and by the data: growth is one number for the whole company, typed 81 times
today by accident of the static file's structure. Seasonality is genuinely per-product — sattu
(1.5) and rice (1.0) really do move differently around festivals — and stays exactly where it is.

## What changes on screen

**Removed** (8 columns — Growth joins the 7 named explicitly in the request, since it becomes
global rather than dropped entirely):

`Growth · Current FBA · Ship Alert · Current WH · Reorder Alert · Rate · Stock Value · Inv Days`

**New column order**, moving `Ideal WH` to sit immediately after the numbers that produce it,
since it is the number being watched:

```
# · Product · Brand · Source · 7d/30d · Last Month Sale · Seasonal · Forecast · Daily ·
Ideal WH (bold) · S→WH · Pack · WH→IXD · IXD→FBA · Lead Total · Ideal FBA · WH Buf Days
```

`Ideal FBA` and `Lead Total` stay, one column later — still useful for planning the downstream
pipeline even without live FBA-stock tracking; they are a *target*, not an *alert*, so they carry
no urgency colouring the way `Ship Alert`/`Reorder Alert` used to.

**Default sort**: `Ideal WH` descending, so the products needing the most warehouse stock are the
first thing on the page, not the 39th row down after a `#` sort.

**KPI strip**, replacing the four alert/value-based stats that no longer have data behind them:

```
Products · Total Forecast (kg/mo) · Total Ideal WH (kg) · Diverged (count)
```

`Total Ideal WH` becomes the headline number for the whole portfolio: "this much kg needs to be
sitting at the warehouse, right now, across every product." `Diverged` surfaces how many rows are
currently running the widened buffer, so a sudden cluster of volatility is visible at a glance
without reading 38 rows for a ⚠.

**Global growth rate**: one new input, alongside the existing blend-settings panel (7-day weight,
divergence threshold) — not a separate UI fixture, since it is conceptually the same kind of thing
("a saved multiplier that shapes every forecast") and the owner already knows where that panel is.

## Schema

`ProjectionRow.growth_rate` is dropped via an Alembic migration — genuinely dead once growth is
global, and this codebase's own convention (see the `ads_performance` table's removal) is to
delete a column that has become unused rather than leave it stored and silently ignored.

`seasonal_impact`, `purchase_rate`, `current_fba_stock`, `current_wh_stock` are **kept** as columns
even though the UI stops reading three of them today (`purchase_rate`, `current_fba_stock`,
`current_wh_stock`) — they hold real data entry (purchase rates, hand-typed stock counts) that
would otherwise be silently discarded, and the request explicitly says "we can think about them
later when we automate stuff." Only genuinely dead data (`growth_rate`, now that it is global) is
actually removed.

The blend settings row (`portfolio_settings`, name `projection_blend`) gains two keys, following
the exact `DEFAULT_BLEND`/`BLEND_RANGES`/`blend_setting_error`/`blend_or_default` pattern already
built for `seven_day_weight`/`divergence_pct`:

| key | default | range | meaning |
|---|---|---|---|
| `global_growth_rate` | `0.3` | `(0.0, 3.0)` | applied to every product's forecast |
| `divergence_buffer_multiplier` | `1.5` | `(1.0, 5.0)` | how much a diverged row's WH buffer widens |

Range floors matter here specifically: `divergence_buffer_multiplier` has a floor of `1.0`, not
`0.0` — a value below 1 would *shrink* the buffer for a volatile product, which is the opposite of
what this setting exists to do, the same class of mistake `good_rating: 99` already taught this
codebase to guard against on read as well as write.

## What does not change

- `logic.blended_daily_rate`, `sales_kg_by_parent`, the None-vs-0.0 handling, the weekly refresh
  job, `daily_range_complete`-style discipline — untouched. This is purely the arithmetic that
  turns an already-correct demand rate into a stock target, and the settings/column layout around
  it.
- The manual-override mechanism (`sales_source="manual"`, `/reset-row`) is untouched; it still
  governs whether `last_month_sale` is hand-typed or sheet-derived. It stops being the switch that
  decides whether seasonality/growth apply — they always apply now.
- `catalogue`-based active-parent filtering, the `needs_review` flag, hidden-parent naming — all
  from the prior change, all untouched.

## Files expected to change

`app/projections/logic.py` (formula), `app/projections/repository.py` (two new blend-setting
keys), `app/routers/projections.py` (drop the removed fields from `/calculate`, `/download`, the
summary block; add `global_growth_rate`/`divergence_buffer_multiplier` to `/blend-settings`),
`app/models.py` + a new Alembic migration (drop `growth_rate`), `templates/projections.html`
(column removal/reorder, KPI strip, growth-rate input, default sort), `deploy/update-ec2.sh`
(no new table, so likely no detector change — confirm during planning), `CLAUDE.md`.

## Verification

**Automated**
- A row's `ideal_wh_stock` reflects `supplier_to_wh` — mutate the lead time, forecast changes.
- A diverged row's effective buffer is `wh_buffer_days × divergence_buffer_multiplier`; a
  non-diverged row's is `wh_buffer_days` unchanged.
- `global_growth_rate` and `seasonal_impact` both apply identically whether `sales_source` is
  `sheet` or `manual` — the exact regression test for the bug this change fixes.
- `divergence_buffer_multiplier` below `1.0` is refused on save and discarded on read (mirroring
  the `good_rating: 99` test already covering `BLEND_RANGES`).
- `/calculate`, `/download`, and the summary no longer reference the nine removed fields;
  `growth_rate` is gone from `ProjectionRow` after the migration.
- Mutation harness: extend `scripts/mutate_projections.py` with entries for the lead-time
  additivity, the divergence multiplier, and the seasonality/growth now-uniform application.

**Manual, on the running app**
Reload `/projections-page` → `Ideal WH` is the first calculated column after Daily, table is
sorted by it descending → a product already known `diverged` in production (Govind Bhog Rice)
shows a widened buffer note and a materially larger `Ideal WH` than before this change → editing
the global growth rate moves every row's forecast, not one → the removed columns are gone from
both the table and the Excel download → KPI strip shows Total Ideal WH and a Diverged count.
