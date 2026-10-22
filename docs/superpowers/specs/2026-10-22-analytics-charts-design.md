# Portfolio → Analytics: charts only, in five sections

22 Oct 2026. Replaces the table-based layout of 19 Oct
(`2026-10-19-analytics-focus-redesign-design.md`).

## Why

*"I still do not like this. In the analytics tab I want only graphs and charts."* The owner picked
all fourteen charts offered, five sections with a switch, every table removed, a period of one
month or the last 3 / 6 / 12 months, and category chips as the only focus.

## Layout

Top, always shown: brand · Show inactive · Refresh · Excel/PDF (unchanged), the KPI tiles, the
category chips (`All` + each category; every chart narrows to the picked one), and the **period
picker**: a month (default the last complete month, back to Nov 2024) plus `1 · 3 · 6 · 12 months`
ending at that month.

A **section switch**, one section on screen, the choice remembered in `localStorage`:

| Section | Charts |
|---|---|
| **Overview** | 1 month on month (as today, measure buttons) · 2 category donut (Revenue / Net / Kg switch) · 3 profit waterfall · 4 top movers |
| **Categories** | 5 share over time (100% stacked area) · 6 Net % by category (one small line chart per category, same y scale) · 7 growth heat map (categories × months, cell = growth on the month) |
| **Products** | 8 treemap (box = product, area = sales, colour = Net %) · 9 Pareto (products by sales, bars + cumulative % line, an 80% guide) · 10 bubble map (x sales, y Net %, size kg, colour category) |
| **Price & ads** | 11 ₹/kg trend by month · 12 ad efficiency map (x TACOS, y sales growth, size sales) · 13 year on year (this month vs the same month a year earlier) |
| **Traffic** | 14 funnel (sessions and units ordered bars, conversion line) · 15 Buy Box watch (only products under 95% in any of the last 12 months) |

The trend charts (1, 5, 6, 7, 11, 13, 14, 15) always show the 12 months ending at the picked month
(fewer when history is shorter); the month to date is added after them when it is the picked month.
The single-period charts (2, 3, 4, 8, 9, 10, 12) show the picked period. The month on month chart is
item 1, so the owner's fourteen picks plus it make fifteen.

**No tables.** Exact figures are in the hover tooltips. Excel/PDF downloads are unchanged.

**Clicking a product** (a treemap box, a bubble, a mover bar, a Pareto bar) opens a product panel
above the section: its monthly sales bars, its ₹/kg line and its Buy Box line, with Close.

## Figures (server, `app/analytics/service.py`)

A new block `period` in the payload for the requested `period_end=YYYY-MM` and `span=1|3|6|12`
(query parameters on `GET /portfolio/analytics`; default: last complete month, span 1):

- `months`: the months summed (only complete months and the month to date if it is the period end);
  `prior_months`: the same number of months immediately before.
- `totals`: sales, refunded, fees, ad_spend, net, units, kg, net_pct, tacos, per_kg, and the same for
  the prior period as `prior`.
- `categories`: per category the same figures plus `share` of sales, `net_share`, `kg_share`.
- `products`: per product key: product, category, sales, net, kg, units, ad_spend, net_pct, tacos,
  prior_sales, `growth` (None when prior sales are 0 or missing).
- `no_prior`: names of products with sales now and none in the prior period (left off the ad map).
- Every rate is computed from the summed rupees, never averaged. kg is None when any summed month's
  kg is unknown.

The waterfall reads `totals`: sales − refunded − fees − ad_spend = net, which holds exactly on every
stored month (measured 22 Oct 2026). Top movers = the 10 largest rises and 10 largest falls in
`sales − prior_sales`. Year on year reads the existing `totals` by month. Buy Box watch reads each
product's `series[m].buy_box`.

## Colours

From the theme only: chart colours through `themeColour()`; the treemap and heat map are HTML,
shaded with `color-mix()` of `--green` / `--red` with `--surface`. One line under the waterfall when
any month of the period has Sponsored Products-only ad spend.

## Tests

- Service: the period totals equal the sum of their months; rates from sums; waterfall identity;
  prior period is the same length immediately before; growth None without prior sales; `no_prior`
  names them.
- Page: the five sections and the period picker exist; no `<table` is rendered by the chart code;
  section switch logic and treemap layout run under Node.
- Browser check of every section on local data; production check after deploy.

## Out of scope

Stored data, refresh, downloads, the Profit tab. No new Amazon calls, no migration.
