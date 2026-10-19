# Analytics focus redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Analytics tab's busy charts with one month-on-month chart, a focus (all / a
category / a product), a month table and a categories table.

**Architecture:** The server adds `per_kg`, category traffic rates, product `tacos`/`per_kg` and a
`category_names` list to the existing `/portfolio/analytics` payload. The page reads one "focus
series" (`data.totals` for all or a category, a product's `series` for a product) and renders the
chart and the month table from it.

**Tech Stack:** FastAPI, Python 3, vanilla JS, Chart.js, pytest, Node for page-logic tests
(`tests/js_harness.run_template_js`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-10-19-analytics-focus-redesign-design.md`.
- ₹/kg = sales (ex-GST) ÷ kg ordered; a dash when kg is unknown or zero.
- Every rate is computed from sums on the server, never averaged from rates.
- A missing figure renders as a dash, never 0. Minus signs are "−".
- Colour lives in `static/theme.css` only (`tests/test_theme.py`); charts read colours via `themeColour`.
- No new Amazon calls, no migration; products table columns and downloads unchanged.
- Never commit `scripts/annotate_fc_recommendation.py` or `reports/`.

---

### Task 1: Server figures

**Files:**
- Modify: `app/analytics/service.py` (`_month_totals`, `_payload`)
- Test: `tests/test_analytics.py`

**Interfaces:**
- Produces: `totals[m]["per_kg"]`; `categories[i]["months"][m]` gains `per_kg`, `sessions`,
  `page_views`, `conversion`, `buy_box`; `products[i]["series"][m]` gains `tacos`, `per_kg`;
  payload `category_names: list[str]` (every category of the brand, biggest last-month sales first).

- [ ] **Step 1: failing tests** (append to `tests/test_analytics.py`)

```python
async def test_per_kg_tacos_and_category_traffic_come_from_sums(stored):
    async with async_session() as db:
        data = await service.build(db, today=TODAY)
    sep = data["totals"]["2026-09"]
    assert sep["per_kg"] == pytest.approx(sep["sales"] / sep["kg"])
    sattu = next(c for c in data["categories"] if c["category"] == "Sattu")["months"]["2026-09"]
    assert sattu["per_kg"] == pytest.approx(sattu["sales"] / sattu["kg"])
    assert sattu["sessions"] == 200 and sattu["conversion"] == pytest.approx(14 / 200)
    assert sattu["buy_box"] == pytest.approx((90 * 120 + 100 * 80) / 200 / 100)
    chana = next(p for p in data["products"] if p["product"] == "Chana Sattu")["series"]["2026-09"]
    assert chana["per_kg"] == pytest.approx(chana["sales"] / chana["kg"])
    assert chana["tacos"] == (chana["ad_spend"] / chana["sales"] if chana["ad_spend"] is not None else None)


def test_per_kg_is_a_dash_when_a_pack_weight_is_unknown():
    known = {"sales": 100.0, "units": 10, "kg": 10.0, "net": 1.0, "fees": 1.0, "refunded": 0, "ad_spend": 0}
    assert service._month_totals([known])["per_kg"] == pytest.approx(10.0)
    assert service._month_totals([known, {**known, "kg": None}])["per_kg"] is None


async def test_every_category_is_named_even_when_one_is_picked(stored):
    async with async_session() as db:
        only = await service.build(db, category="Rice", today=TODAY)
    assert set(only["category_names"]) == {"Sattu", "Rice"}
```

- [ ] **Step 2:** `venv/Scripts/python -m pytest -q tests/test_analytics.py -k "per_kg or category_is_named"` → FAIL (KeyError `per_kg`).

- [ ] **Step 3: implement** in `app/analytics/service.py`:

In `_month_totals`, after the existing `out.update(...)`:
```python
    out["per_kg"] = logic.pct(sales, out["kg"])
```
In `_payload`, when building `cats`, also collect traffic, and add rates per category month:
```python
    cats, cat_traffic = {}, {}
    for p in rows:
        for m, e in p["months"].items():
            cats.setdefault(p["category"], {}).setdefault(m, []).append(e)
        for m, trs in (p.get("traffic") or {}).items():
            cat_traffic.setdefault(p["category"], {}).setdefault(m, []).extend(trs)
```
and inside the per-month category loop, after `t = _month_totals(...)`:
```python
            t.update(logic.weighted_rates(cat_traffic.get(name, {}).get(m, [])))
```
In the product `series` dict comprehension add:
```python
                      "tacos": logic.pct(e["ad_spend"], e["sales"]),
                      "per_kg": logic.pct(e["sales"], e.get("kg")),
```
And `category_names` in the returned dict, from every product of the brand (not the category filter):
```python
    brand_rows = [p for p in base.values() if brand == ALL_BRANDS or p["brand"] == brand]
    cat_sales: dict[str, float] = {}
    for p in brand_rows:
        cat_sales[p["category"]] = cat_sales.get(p["category"], 0.0) + ((p["months"].get(last) or {}).get("sales") or 0.0)
    ...
        "category_names": sorted(cat_sales, key=lambda c: -cat_sales[c]),
```

- [ ] **Step 4:** same pytest command → PASS; then `venv/Scripts/python -m pytest -q tests/test_analytics.py` → all pass.
- [ ] **Step 5:** commit `feat(analytics): per-kg, category traffic and product TACOS in the payload`.

### Task 2: The page

**Files:**
- Modify: `templates/portfolio_analytics.html`
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: Task 1 fields.
- Produces (page functions, tested under Node): `focusSeries() -> {label, months}`,
  `tableMonths() -> [month]` (newest first, month to date first when held), `changeHtml(kind, good, now, before)`,
  `sparkline(values) -> svg string`, const `MEASURES` (`[key, label, kind, good]`, kind in
  `money|count|kg|pct|perkg|rate`, good in `1|-1|0`).

- [ ] **Step 1: failing tests** — replace the two layout tests added on 19 Oct
  (`test_the_growth_split_table_is_gone_and_categories_are_a_chart`,
  `test_every_trend_chart_has_a_view_switch_a_readout_and_follows_the_range`) with:

```python
def test_only_the_month_on_month_chart_is_left_and_the_tables_replace_the_rest():
    for gone in ('id="cat-chart"', 'id="cost-chart"', 'id="funnel-chart"', 'id="kg-chart"',
                 'id="detail-chart"', "Why sales moved", "renderGrowth"):
        assert gone not in PAGE, gone
    for present in ('id="mom-chart"', 'id="month-table"', 'id="cat-table"', 'id="show-bar"',
                    'id="range"', 'id="product-pick"'):
        assert present in PAGE, present


FOCUS_FNS = ["esc", "whole", "chartMonths", "focusSeries", "tableMonths", "changeHtml"]


def _page_js(body):
    from tests.js_harness import run_template_js
    return run_template_js(ROOT / "templates" / "portfolio_analytics.html", FOCUS_FNS, [],
                           "let range = '12'; let picked = null; let category = '';\n" + body)


def test_the_focus_reads_the_totals_or_the_picked_products_series():
    out = _page_js("""
      data = {totals: {"2026-09": {sales: 10}}, products: [{key: "P", product: "Chana", series: {"2026-09": {sales: 4}}}],
              complete_months: ["2026-09"], months: ["2026-09"], mtd: null};
      const a = focusSeries(); picked = "P"; const b = focusSeries(); category = "Rice"; picked = null; const c = focusSeries();
      emit([a.label, a.months["2026-09"].sales, b.label, b.months["2026-09"].sales, c.label]);""")
    assert out == ["all products", 10, "Chana", 4, "Rice"]


def test_the_month_table_is_newest_first_with_the_month_to_date_on_top():
    out = _page_js("""
      data = {complete_months: ["2026-07", "2026-08", "2026-09"], months: ["2026-07", "2026-08", "2026-09", "2026-10"],
              mtd: {month: "2026-10", days: 16}};
      range = "2"; emit(tableMonths());""")
    assert out == ["2026-10", "2026-09", "2026-08"]


def test_a_change_is_coloured_by_whether_up_is_good():
    out = _page_js("""emit([changeHtml("money", 1, 110, 100), changeHtml("pct", -1, 0.30, 0.25),
                             changeHtml("money", 0, 90, 100), changeHtml("money", 1, 5, null)]);""")
    assert 'class="pos"' in out[0] and "+10%" in out[0]
    assert 'class="neg"' in out[1] and "+5 pts" in out[1]
    assert "−10%" in out[2] and "pos" not in out[2] and "neg" not in out[2]
    assert out[3] == ""
```

- [ ] **Step 2:** `venv/Scripts/python -m pytest -q tests/test_analytics.py -k "focus or month_table or coloured or only_the"` → FAIL.

- [ ] **Step 3: implement.** In `templates/portfolio_analytics.html`:
  - Markup: delete the range bar, the categories / cost / funnel / kg cards and the `#detail` card.
    Insert above the month-on-month card a `#show-bar` card (category chips `#show-cats`, product
    `<input id="product-pick" list="product-list">` + `<datalist id="product-list">`, `#range`
    6/12/All seg, `#show-label`); extend `#metric` with units, kg, net, net_pct, tacos, ad_spend,
    per_kg, sessions, conversion buttons; after the chart add a card with `#month-table` and
    `#detail-prices`, then a card with `#cat-table`.
  - JS: remove `view`, the readout helpers, `renderCatChart`, `renderCostChart`, `renderFunnelChart`,
    `renderKgChart`, `renderDetail`; add:

```js
const MEASURES = [
  ["sales", "Sales", "money", 1], ["units", "Units", "count", 1], ["kg", "Kg", "kg", 1],
  ["net", "Net ₹", "money", 1], ["net_pct", "Net %", "pct", 1], ["tacos", "TACOS", "pct", -1],
  ["ad_spend", "Ad spend", "money", 0], ["per_kg", "₹/kg", "perkg", 1],
  ["sessions", "Sessions", "count", 1], ["conversion", "Conversion", "rate", 1], ["buy_box", "Buy Box", "pct", 1],
];
function focusSeries(){
  const p = picked && (data.products || []).find(x => x.key === picked);
  if(p) return {label: p.product, months: p.series || {}};
  return {label: category || "all products", months: data.totals || {}};
}
function tableMonths(){
  const out = chartMonths(data.complete_months).slice().reverse();
  return data.mtd ? [data.mtd.month, ...out] : out;
}
/* The change on the month: % for amounts, points for rates; coloured only when up is good or bad. */
function changeHtml(kind, good, now, before){
  if(now == null || before == null) return "";
  let d, text;
  if(kind === "pct" || kind === "rate"){
    const f = kind === "rate" ? 10 : 1;
    d = Math.round((now - before) * 100 * f) / f;
    text = `${d > 0 ? "+" : d < 0 ? "−" : "±"}${Math.abs(d)} pts`;
  } else {
    if(!before) return "";
    d = Math.round((now / before - 1) * 100);
    text = `${d > 0 ? "+" : d < 0 ? "−" : "±"}${Math.abs(d)}%`;
  }
  const cls = !good || d === 0 ? "" : (d > 0) === (good > 0) ? "pos" : "neg";
  return ` <span class="chg${cls ? " " + cls : ""}"${cls ? "" : ""}>${text}</span>`.replace(' class="chg"', ' class="chg"');
}
```
  (implementation may simplify the return to `` ` <span class="${cls}">${text}</span>` `` — the tests
  look for `class="pos"` / `class="neg"` and the text.)

  plus `fmtMeasure(kind, v)` (money → `money`, count → `num`, kg → `kg`, pct → `pct`, perkg → `"₹"+whole`,
  rate → one decimal %), `renderShowBar()`, `renderMonthTable()` (rows from `tableMonths()`, each
  measure cell = value + `changeHtml` against the previous complete month in `focusSeries().months`;
  month to date row class `mtd` with no change), `renderCatTable()` (hidden when `picked`; header =
  range months, first row Total from `data.totals`, rows from `data.categories`, last two cells a
  `sparkline` of the current `metric` and the change on the month; row click sets `category`),
  `renderPrices()` (the pack-price table for a picked product, into `#detail-prices`), and
  `sparkline(values)` (an 80×20 inline SVG polyline, `stroke="currentColor"`, nulls skipped).
  `renderMomChart` reads `focusSeries().months[m][metric]` for the shown months and formats the axis
  by the measure's kind. Products-table row click sets `picked` and scrolls to `#show-bar`.

- [ ] **Step 4:** pytest from Step 2 → PASS; `venv/Scripts/python -m pytest -q tests/test_analytics.py tests/test_theme.py tests/test_template_render_targets.py tests/test_local_dates.py` → PASS.
- [ ] **Step 5:** browser check (preview `tracker`, local DB): all / Sattu / one product, each measure
  button, 6/12/All; month table changes coloured; categories table click focuses. Commit
  `feat(analytics): one chart, a focus, a month table and a categories table`.

### Task 3: Docs, suite, deploy

- [ ] Update the Analytics section of `CLAUDE.md` (replace the 19 Oct "Screen layout" bullet) and the test count.
- [ ] `venv/Scripts/python -m pytest -q` → all pass; `venv/Scripts/python scripts/mutate_analytics.py` → "All N mutations caught".
- [ ] Commit, push, deploy (`echo y | bash deploy/update-ec2.sh` on the box), check the page returns 200
  and the payload carries `per_kg` and `category_names`.
