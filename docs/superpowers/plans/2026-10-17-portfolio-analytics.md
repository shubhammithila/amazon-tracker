# Portfolio → Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (this project is inline
> only). Steps use checkbox (`- [ ]`) syntax.

**Goal:** A fifth Portfolio sub-tab showing Nov 2024 → now, month by month, for today's active
products (toggle for inactive): MoM growth, selling price, Net, categories, growth split, cost trends,
traffic funnel, kg and concentration.

**Architecture:** `app/analytics/` stores one row per (month, child ASIN) of economics (Amazon's
`MONTH` aggregation, written through the Profit tab's `size_row`) and of Sales & Traffic. The service
rebuilds Amazon-shaped rows per month (adding Sponsored Brands from `economics_daily` where held) and
runs them through the Profit tab's own `logic.portfolio`, so products, flavours, Active and Net are
the Profit tab's by construction; pure functions in `analytics/logic.py` then compute MoM, the growth
split, concentration and weighted rates. The page reads one cached payload per (brand, category,
inactive) and draws with the bundled Chart.js.

**Tech Stack:** FastAPI, async SQLAlchemy/SQLite, Alembic, Data Kiosk (`economics._run_query`),
APScheduler, vanilla JS + `/static/chart.min.js`, pytest + Node harness.

Spec: `docs/superpowers/specs/2026-10-17-portfolio-analytics-design.md`.

## Global Constraints

- Read-only towards Amazon; no write call in `app/analytics/`.
- Net = the Profit tab's Net (ex-GST fees, SP ads, plus SB where `economics_daily` holds it).
- A missing figure is `None` (a dash), never 0. Percentages from sums; conversion = Σunits/Σsessions,
  Buy Box = page-view-weighted.
- Complete months drive growth; the current month is month-to-date and excluded from MoM.
- Growth split: volume + price + mix == Δ sales exactly.
- Nightly job `analytics_refresh` at **09:00 IST** via `ist.utc_hhmm`, same flags as the portfolio job.
- New migration adds a detector branch, newest first, and its tables to the required set.
- No colour literals in templates (theme variables only, read at runtime for Chart.js).
- Commits end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`; never commit
  `scripts/annotate_fc_recommendation.py` or `reports/`.

## Files

| File | Role |
|---|---|
| `app/models.py` | `AnalyticsMonthly`, `AnalyticsTrafficMonthly`, `AnalyticsRefresh` |
| `alembic/versions/b8e4d1f27c60_analytics_monthly.py` | the three tables |
| `deploy/update-ec2.sh` | detector branch + required tables |
| `app/analytics/fetch.py` | `economics_months(start, end)`, `traffic_month(month)` |
| `app/analytics/repository.py` | `rows_from_economics`, `save_economics`, `save_traffic`, `load_months`, `months_held`, `sb_by_month`, `record_run`, `last_run` |
| `app/analytics/logic.py` | `month_range`, `pct`, `growth_split`, `concentration`, `weighted_rates`, `product_series` |
| `app/analytics/service.py` | `build(db, brand, category, include_inactive)` cached payload |
| `app/analytics/refresh.py` | `run(months)`, `run_recent()`, `STATE` |
| `app/routers/analytics.py` | `GET /portfolio/analytics`, `POST /portfolio/analytics/refresh`, `GET /portfolio/analytics/refresh-status`, `POST /portfolio/analytics/export` |
| `templates/portfolio_analytics.html`, `templates/_portfolio_tabs.html`, `app/main.py` | the page and tab |
| `app/scheduler.py` | 09:00 IST job |
| `scripts/backfill_analytics.py` | one-off history |
| `tests/test_analytics.py`, `scripts/mutate_analytics.py` | tests |

---

### Task 1: Storage

- [ ] Models (`month` String(7) `YYYY-MM`; UNIQUE `(month, child_asin)` on both data tables):
  - `AnalyticsMonthly`: month, child_asin, parent_asin, ordered_sales, refunded_sales, ad_spend,
    net_proceeds (Numeric 12,2), units_ordered, units_refunded, net_units (Integer), fees_json,
    ads_json (Text), fetched_at.
  - `AnalyticsTrafficMonthly`: month, child_asin, parent_asin, sessions, page_views, units_ordered
    (Integer), buy_box_pct (Numeric 6,2), sales (Numeric 12,2), fetched_at.
  - `AnalyticsRefresh`: id, started_at, finished_at, status (running|done|failed), source, months
    (Text, comma list), error.
- [ ] Migration `b8e4d1f27c60` (down `a7d3c9e15b20`); detector `elif "analytics_monthly" in tables:
  print("b8e4d1f27c60")` above the `fba_inventory_refresh` branch; tables in the `need` set.
- [ ] `pytest tests/test_schema_migrations.py` passes; local `alembic upgrade head`; commit.

### Task 2: Fetch + repository (tests first)

- [ ] `fetch.economics_months(start, end, run=None)`: `economics.build_query(start, end, market,
  by_day=False)` with `date: RANGE` → `date: MONTH`, run via `economics._run_query` (injectable
  `run`). Returns raw rows.
- [ ] `fetch.traffic_month(month, today, run=None)`: Sales & Traffic `salesAndTrafficByAsin`,
  `aggregateBy: CHILD`, start = first day, end = min(last day, yesterday). Raw rows.
- [ ] `repository.rows_from_economics(raw)` → dicts keyed (month, asin) via `portfolio.logic.size_row`
  (so fees/net are ex-GST exactly as the Profit tab stores them); rows with no `startDate` skipped.
- [ ] `save_economics(db, rows, months)` / `save_traffic(db, rows, month)`: delete the months written,
  bulk insert. Traffic `unitSessionPercentage` not stored (derived).
- [ ] `load_months(db)` → `{month: [amazon-shaped row]}` (reusing `_summed_amazon_row`'s shape) with
  SB: `sb_by_month(db)` sums `economics_daily.sb_spend` per (month, child) at ASIN grain and returns
  `{month: {"sb": {asin: x}, "days": n_days_with_sb}}`; SB added to ads and taken off net.
- [ ] `load_traffic(db)` → `{month: {asin: {...}}}`; `months_held(db)`; `record_run`, `last_run`.
- Tests: size_row parity (stored month equals what `_rows_to_daily` would store for the same raw
  row), delete-then-insert replaces a month, SB folded in (ad +, net −), dateless row skipped.

### Task 3: Pure logic (tests first)

- [ ] `month_range(first, last)`; `pct(part, whole)` → None when whole is 0/None.
- [ ] `growth_split(prev_sizes, cur_sizes)` where sizes = `{asin: {"sales", "units", "kg"}}`
  (units = units ordered, kg = weight ordered or None):
  - ΔS = S1 − S0; price = Σ over sizes with units in both months of (p1 − p0)·u1, p = sales/units;
  - volume = (K1 − K0)·(S0/K0) when every size with units has a weight and K0 > 0, else
    (U1 − U0)·(S0/U0) when U0 > 0, else ΔS − price;
  - mix = ΔS − price − volume. Returns rounded dict; sums exactly (mix absorbs rounding).
- [ ] `concentration(sales_by_key, n=10)` → share of the top n (None when total 0).
- [ ] `weighted_rates(traffic_rows)` → sessions, page_views, conversion = units/sessions,
  buy_box = Σ(bb·pv)/Σpv.
- Tests: split sums exactly with a size present in only one month; price effect sign; a new product
  is all volume; concentration changes when the top 10 change; conversion ≠ plain average.

### Task 4: Service

- [ ] `build(db, brand=None, category=None, include_inactive=False)`:
  1. version = (max fetched_at both tables, last run id, catalogue source) → cache clear.
  2. per month: `logic.portfolio(rows, catalogue, {}, {}, include_inactive=…, flavour_keys=…)` (in
     `asyncio.to_thread`), cached per (month, include_inactive) under the version.
  3. product series keyed by parent row id: product, brand, category (`category_of`), active,
     `months[m]` = sales, units_ordered, net_units, kg (weight_ordered_kg), net, fees_total,
     refunded, ad_spend, sizes `{asin: {label, sales, units, kg, asp}}`; traffic mapped
     child→row key (flavour key, else parent, else child).
  4. filter brand/category; totals per month (sums; pct from sums; SB status full/partial/none);
     MoM % on complete months; growth split per month (Σ over products); categories × months;
     top-10 share; products table fields (last complete month sales & MoM, ASP per size now / 6
     months earlier / min / max, Net % and change, sessions, conversion, buy box).
  5. `months`, `complete_months`, `mtd` (month, days), `brands`, `categories_list`, `last_run`.
- Tests: totals equal sum of products; category total equals its rows; inactive toggle; MTD
  excluded from MoM; a month equals `logic.portfolio` on the same rows (Net parity).

### Task 5: Refresh, scheduler, backfill, router, page

- [ ] `refresh.run(months, source)`: economics per quarter span then traffic per month; records the
  run; `finally` clears `STATE["running"]`. `run_recent()` = current + previous month.
- [ ] Scheduler `ANALYTICS_REFRESH_IST = (9, 0)`, job `analytics_refresh` (test the id and IST time).
- [ ] `scripts/backfill_analytics.py [--from 2024-11]`: months not yet held, oldest first, quarter
  chunks; stops on failure naming the month.
- [ ] Router (Portfolio area): GET payload, POST refresh (409 when running), GET status, POST export
  (xlsx/pdf of products table via `portfolio.export` writers).
- [ ] Page `templates/portfolio_analytics.html` + tab + `/portfolio-page/analytics`: brand select,
  inactive toggle, Refresh, category cards, KPI tiles, metric selector + bar chart with MoM labels,
  growth-split table, categories grid, products table (sort/search, row click → detail charts),
  cost-trend chart, funnel chart, kg stacked chart + top-10 line. Chart colours from CSS variables.
- Tests: route auth, 409, page renders targets, theme rules, sub-tab link.

### Task 6: Verify, mutate, document, deploy

- [ ] `scripts/mutate_analytics.py` (split sums, SB fold, MTD exclusion, weighted rates, inactive
  filter, dash-not-zero, job time): All N mutations caught.
- [ ] Full suite green; CLAUDE.md section; commit; push; deploy (migration: check out the script
  first); `alembic current`; run the backfill on production in `screen`; check Oct 2025 against
  the probe (₹22.9L economics) and last complete month against the Profit tab's same window; view the
  page in the browser.
