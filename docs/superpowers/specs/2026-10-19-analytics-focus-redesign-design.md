# Portfolio → Analytics: one chart, a focus, and clean tables

19 Oct 2026. Replaces the chart section of the Analytics tab shipped on 17-19 Oct.

## Why

Asked for as *"apart from the month on month graph, rest of the graphs I am not liking. either the UI or
the concept."* The owner's answers:

- the other charts are **too busy** (several measures, two axes, six colours on one chart);
- he wants to **focus on one category or one product** rather than see every category at once;
- he wants **exact numbers in clean tables** rather than charts.

The measures he tracks month by month: **sales, units, kg, Net ₹, Net %, TACOS, ad spend, ₹/kg**.
Traffic (sessions, conversion, Buy Box) stays as **extra columns**.

The month-on-month bar chart is the one he likes: one measure, plain bars, a switch for the measure.
Everything below follows that pattern.

## The page, top to bottom

1. **Controls, KPI tiles, category cards** — unchanged.
2. **Show bar**:
   - category chips: `All` and one per category (the same filter the category cards already apply;
     the two stay in step);
   - a product search (a `<datalist>` over the products in the payload). Choosing one focuses the page
     on that product;
   - the range switch `6 months · 12 months · All` (12 by default);
   - a label naming the focus: "Showing: all products", "Showing: Sattu", "Showing: Chana Sattu ×"
     (× clears the product).
3. **Month-on-month chart** — unchanged in style (single bar series, month to date in grey). Measure
   buttons: Sales · Units · Kg · Net ₹ · Net % · TACOS · Ad spend · ₹/kg · Sessions · Conversion.
   It follows the focus and the range.
4. **Month table** for the focus, newest month first, one row per month in the range plus the month to
   date (greyed, no change figures). Columns:
   Sales · Units · Kg · Net ₹ · Net % · TACOS · Ad spend · ₹/kg · Sessions · Conversion · Buy Box.
   - rupee and count columns carry their change on the month in % beside the figure;
   - percentage columns carry the change in points; colour by direction of good (Net %, conversion,
     Buy Box up = green; TACOS up = red; ad spend has no colour, it is neither good nor bad by itself);
   - a missing figure is a dash, never 0.
5. **Categories table** (shown only when all products are shown): one row per category, the months of the
   range as columns for the chart's current measure, a small inline SVG trend line, and the change on
   the month. Clicking a row focuses that category. A total row on top.
6. **Products table** — unchanged columns. Clicking a row now FOCUSES the page on that product
   (replacing the separate detail panel); its pack prices by month appear as a table under the month
   table while it is focused.

**Removed**: the categories line chart, cost trends, traffic funnel and kilograms-by-category charts,
their readout lines and view switches, and the product detail chart.

## Figures

- **₹/kg = sales (ex-GST) ÷ kg ordered.** A dash when kg is unknown (a pack with no weight in the
  month) or zero.
- **TACOS = ad spend ÷ sales**, as now.
- All of these are computed on the SERVER from sums, never averaged from rates:
  - `totals[m]` already carries tacos and traffic; gains `per_kg`;
  - each `categories[].months[m]` gains `per_kg` and the traffic rates (`sessions`, `conversion`,
    `buy_box`, weighted the way the totals are);
  - each `products[].series[m]` gains `tacos` and `per_kg` (it already has traffic and `net_pct`).
- The focus decides which series the chart and month table read:
  all products / a category → `totals` (the server already narrows totals to `?category=`);
  a product → that product's `series`. Changes on the month are computed in the page from consecutive
  complete months of that series.
- The products table, the Excel/PDF downloads and the growth split in the payload are unchanged.

## Tests

- Template: the removed chart ids and functions are absent; the month table, categories table, show
  bar and range switch are present; the products table click focuses rather than opening a panel.
- Service: `per_kg` is sales ÷ kg and None when kg is unknown; a category's traffic rates are weighted
  from its products' rows; a product series carries `tacos` and `per_kg`.
- Page logic run under Node (`tests/js_harness.py` pattern): the focus picks the right series, the
  month table's change on the month uses the previous complete month, and the month to date has none.
- Browser check on local data; production check after deploy.

## Out of scope

The tiles, category cards, products table columns, downloads, refresh, and the stored data. No new
Amazon calls and no migration.
