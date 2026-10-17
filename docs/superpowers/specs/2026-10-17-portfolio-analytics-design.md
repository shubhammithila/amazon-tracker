# Portfolio → Analytics: two years of monthly history for the active range

Date: 17 Oct 2026. Status: design approved (approach A), not built.

## Why

Asked for as *"In the portfolio I want an analytics tab… all the data from the beginning like 2 years
back or whenever the account started, but only for the active products… month on month growth,
selling price variations, profit variations, category wise insights. Also research what analytics we
can add."*

## What history exists (measured 17 Oct 2026, live, read-only)

| Source | Earliest month | Notes |
|---|---|---|
| Data Kiosk `analytics_economics_2024_03_15` | **Nov 2024** (₹14,341; Dec 2024 ₹1,15,934) | Oct 2024 is refused (FATAL): Amazon keeps ~2 years. `aggregateBy.date: MONTH` and `WEEK` both work |
| Data Kiosk `analytics_salesAndTraffic_2024_04_24` | **Nov 2024** (8,678 sessions) | `salesAndTrafficByAsin` returns one row per child ASIN for the requested range, so per month means one query per month |
| Shipments report (Repeat / Customer value) | ~18 months | Not used by this tab |
| `pricing_data.json` purchase rates | today only | Not used: the owner chose Net as on the Profit tab |

So "since the account started" is **Nov 2024 to now**, about 24 months, fetchable once.

## Decisions (the owner's)

1. **Approach A**: a separate monthly store, backfilled once and topped up nightly. The Profit tab's
   90-day daily store is untouched.
2. **Profit = Net, as on the Profit tab** (sales − refunds − Amazon fees ex-GST − ads), pre-COGS.
3. **Monthly grain throughout**, selling price included.
4. **Active today, with a toggle**: the default view is today's MRP-sheet-active products across all
   24 months; "Show inactive" adds discontinued products back.
5. **First version includes**: month-on-month growth, selling price, Net, category insights, the
   traffic funnel, the growth split, cost trends, kg sold and concentration.

## Data

### Tables (one migration, plus a branch in `deploy/update-ec2.sh`'s detector, newest first)

- **`analytics_monthly`**, UNIQUE `(month, child_asin)`: `month` (`YYYY-MM`), `child_asin`,
  `parent_asin`, `units_ordered`, `units_refunded`, `net_units`, `sales`, `refunded`, `fees_json`
  (ex-GST, by fee name), `ad_spend` (Amazon's SP charge), `net_proceeds` (ex-GST basis, as
  `size_row` produces it), `average_selling_price`, `fetched_at`.
- **`analytics_traffic_monthly`**, UNIQUE `(month, child_asin)`: `sessions`, `page_views`,
  `buy_box_pct`, `unit_session_pct`, `units_ordered`, `sales`, `fetched_at`.
- **`analytics_refresh`**: one row per run (window, status, error, rows) so the screen can say how
  old the figures are and why a run failed.

Rows are written through the Profit tab's own conversion (`portfolio.logic` fee parts, ex-GST), so a
month here and the same days on the Profit tab are computed by one rule. Each write replaces the
months it covers (delete-then-insert scoped by month), so a re-run corrects rather than doubles.

### Sponsored Brands

Economics carries Sponsored Products spend only. For the months the Profit tab's `economics_daily`
still holds `sb_spend` (its last ~60–90 days), that SB figure is added to ad spend and taken off Net,
exactly as the Profit tab does. Older months are **SP only** and are marked so on screen; their TACOS
and Net % read slightly better than reality (SB is ~28% of today's spend).

### Fetching

- **Backfill** (`scripts/backfill_analytics.py`, run once in `screen`): economics with
  `date: MONTH`, in spans Amazon accepts (to be measured; quarterly chunks if a 24-month span is
  refused), and traffic one query per month (24 queries, ~30 s each). Resumable: a month already
  stored is skipped. A failed chunk stops the run and names the month.
- **Nightly** job `analytics_refresh` at **09:00 IST** (after portfolio 07:30, FBA stock 07:45, ads
  08:00), on the same flags as the portfolio job: re-reads the **current and previous month** for
  both sources, because fees post late.
- **Refresh button** on the tab: the same run, in the background, with a progress note; 409 if one is
  already running.
- Read-only towards Amazon; a source test forbids write calls in `app/analytics/`.

## The screen — `/portfolio-page/analytics`

A fifth Portfolio sub-tab (`_portfolio_tabs.html`), Portfolio area permission. Products are the Profit
tab's rows: parent ASIN, flavours split via `flavour_keys`, categories via `category_of`, Active from
the MRP sheet. Brand selector (incl. All brands) and category cards as on the other sub-tabs.

**Complete months drive every growth figure.** The current month is shown separately as **month to
date** with its day count, so a half month never reads as a 50% fall.

1. **KPI tiles** (last complete month): Sales (ex-GST), MoM %, Net %, TACOS, kg sold.
2. **Month-on-month chart**: bars of Sales / Units / Kg / Net (a selector), MoM % on each bar;
   category cards filter it.
3. **Growth split**: each month's Δ sales = **volume** + **price** + **mix**, summing exactly:
   - per product (parent row): volume = Δkg × last month's ₹/kg;
   - price = Σ over pack sizes of (this month's ASP − last month's ASP) × this month's units;
   - mix = the remainder (a shift between pack sizes at different ₹/kg).
   A pack size with no sales in one of the two months contributes to volume/mix only.
4. **Category insights**: categories × months grid of sales, MoM % and share of total, shaded by
   growth; Unclassified kept as its own row.
5. **Products table**: last month's sales and MoM %; ASP last month, 6 months earlier, lowest and
   highest month; Net % and its change; sessions, conversion %, Buy Box %. Sortable, searchable,
   frozen name column. Clicking a product opens its monthly lines (price, units, Net %, conversion).
   Price is shown per pack size inside that panel, since a parent's ASP mixes sizes.
6. **Cost trends**: Amazon fee %, refund %, TACOS, per month (account, category or product).
7. **Traffic funnel**: sessions → conversion % → Buy Box % per month, account or product.
8. **Kg and concentration**: kg sold per category per month (stacked), and the share of sales held by
   the top 10 products per month.

Downloads: Excel and PDF of the products table and the monthly grid as on screen (same writers as
the other sub-tabs). Whole numbers on screen, as on Profit and Projections.

## Rules

- **Net = the Profit tab's Net**; percentages are recomputed from rupee sums, never averaged.
- **A missing figure is a dash, never 0**: a month before a product launched, a missing weight,
  a month with no traffic row.
- Conversion and Buy Box % for a group are session-weighted, never averaged.
- Totals are the sum of the rows on screen; the inactive toggle re-fetches from the server.
- The month boundary is the IST calendar month (Amazon's own monthly aggregation).

## Code layout

- `app/analytics/fetch.py` — the two Data Kiosk queries (reusing `economics._run_query`).
- `app/analytics/repository.py` — store, load, coverage per month.
- `app/analytics/logic.py` — pure: monthly rollup to Profit rows, MoM, growth split, concentration,
  session-weighted rates.
- `app/analytics/service.py` — the payload, cached per data version (latest `fetched_at`, catalogue).
- `app/analytics/refresh.py` — nightly and button runs; `finally` clears the running flag.
- `app/routers/analytics.py`, `templates/portfolio_analytics.html` (Chart.js), scheduler job,
  migration, backfill script.

## Testing

- Growth split: volume + price + mix == Δ sales exactly, on fixtures with unequal sizes and a size
  that appears in only one month.
- Totals equal the sum of rows; category totals equal their rows; concentration on a fixture where
  the top 10 change between months.
- A stored month reproduces the Profit tab's figures for the same days (sales, fees, Net) on a
  fixture run through both paths.
- Session-weighted conversion differs from a plain average on the fixture.
- Active toggle; dash cases; MTD excluded from growth.
- Nightly job id and IST time; no write calls in `app/analytics/`; detector branch.
- `scripts/mutate_analytics.py`: All N mutations caught.
- Production: the backfill's Oct 2025 sales against Seller Central's Business Report for Oct 2025
  (expected ~5% lower, ex-GST), and the last complete month against the Profit tab's same window.

## Out of scope

- Purchase-cost profit (COGS).
- Weekly prices, YoY (possible from Nov 2026), elasticity, new-product ramp, Easy Ship vs FBA share,
  repeat-customer revenue: candidates for a second version.
- Any write to Amazon.
