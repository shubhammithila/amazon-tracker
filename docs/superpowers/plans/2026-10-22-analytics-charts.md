# Analytics charts-only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Analytics tab's tables with fifteen charts in five sections, driven by a
period picker (a month, or 3 / 6 / 12 months ending there) and category chips.

**Architecture:** `service._period()` sums the picked period and the equal period before it, per
product, per category and overall, from the per-month entries `_compute_base` already builds; the
route takes `period_end` and `span`. The page draws only: Chart.js 4.4.4 for bars, lines, areas,
doughnut, floating-bar waterfall and bubbles; plain HTML for the treemap and heat map.

**Tech Stack:** FastAPI, Python 3, vanilla JS, Chart.js 4.4.4 (`static/chart.min.js`), pytest, Node
via `tests/js_harness.run_template_js`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-10-22-analytics-charts-design.md`.
- No tables on the page; exact figures in tooltips. Excel/PDF unchanged.
- Every rate from summed rupees on the server. A missing figure is a dash / left off, never 0.
- Colour only from `static/theme.css` (`themeColour()`, `color-mix()` of theme variables); no hex or
  rgba literals in the template (`tests/test_theme.py`).
- Trend charts: the 12 months ending at the picked month (+ month to date when it is picked).
- No new Amazon calls, no migration. Never commit `reports/` or `scripts/annotate_fc_recommendation.py`.

---

### Task 1: The period block on the server

**Files:**
- Modify: `app/analytics/service.py` (new `SPANS`, `_period`, `_payload` and `build` signatures)
- Modify: `app/routers/analytics.py` (GET passes `period_end`, `span`)
- Test: `tests/test_analytics.py`

**Interfaces:**
- Produces: `payload["period"]` =
  `{"end": "YYYY-MM", "span": int, "months": [..], "prior_months": [..] | [],
    "totals": {...figures, "prior": {...figures}}, "categories": [{"category", ...figures, "share",
    "net_share", "kg_share"}], "products": [{"key", "product", "category", ...figures,
    "prior_sales", "growth"}], "no_prior": [names], "sp_only": bool}`
  where figures = `_month_totals` keys (sales, units, kg, net, fees, refunded, ad_spend, net_pct,
  fees_pct, refunds_pct, tacos, per_kg). `payload["period_choices"]` = complete months + the month to
  date, newest first.
- `build(db, *, brand, category, include_inactive, today, period_end=None, span=1)`.

- [ ] **Step 1: failing tests** (fixture `stored`: Aug + Sep complete, Oct to date)

```python
async def test_the_period_sums_its_months_and_compares_the_same_length_before(stored):
    async with async_session() as db:
        one = await service.build(db, today=TODAY)
        two = await service.build(db, today=TODAY, period_end="2026-09", span=3)
    p = one["period"]
    assert p["end"] == "2026-09" and p["months"] == ["2026-09"] and p["prior_months"] == ["2026-08"]
    assert p["totals"]["sales"] == pytest.approx(one["totals"]["2026-09"]["sales"])
    assert p["totals"]["prior"]["sales"] == pytest.approx(one["totals"]["2026-08"]["sales"])
    assert p["totals"]["net_pct"] == pytest.approx(p["totals"]["net"] / p["totals"]["sales"])
    t = p["totals"]
    assert t["sales"] - t["refunded"] - t["fees"] - t["ad_spend"] == pytest.approx(t["net"], abs=0.05)
    q = two["period"]
    assert q["months"] == ["2026-08", "2026-09"] and q["prior_months"] == []   # history is short
    assert q["totals"]["sales"] == pytest.approx(2000 + 2720)
    assert all(x["growth"] is None for x in q["products"])


async def test_period_products_and_categories_carry_growth_shares_and_no_prior(stored):
    async with async_session() as db:
        p = (await service.build(db, today=TODAY))["period"]
    chana = next(x for x in p["products"] if x["product"] == "Chana Sattu")
    assert chana["prior_sales"] == pytest.approx(1000.0) and chana["growth"] == pytest.approx(0.72)
    by = {c["category"]: c for c in p["categories"]}
    assert by["Sattu"]["share"] == pytest.approx(1720 / 2720)
    assert sum(c["share"] for c in p["categories"]) == pytest.approx(1.0)
    assert p["no_prior"] == []


async def test_a_period_end_not_held_falls_back_to_the_last_complete_month(stored):
    async with async_session() as db:
        p = (await service.build(db, today=TODAY, period_end="2019-01", span=7))["period"]
    assert p["end"] == "2026-09" and p["span"] == 1
```

- [ ] **Step 2:** `venv/Scripts/python -m pytest -q tests/test_analytics.py -k period` → FAIL (KeyError `period`).

- [ ] **Step 3: implement** in `app/analytics/service.py`:

```python
SPANS = (1, 3, 6, 12)


def _period(rows, months, complete, mtd, period_end, span, totals_by_month) -> dict | None:
    """The picked period summed, and the same number of months immediately before it."""
    held = complete + ([mtd] if mtd else [])
    if period_end not in held:
        period_end = complete[-1] if complete else (held[-1] if held else None)
    if period_end is None:
        return None
    span = span if span in SPANS else 1
    i = months.index(period_end)
    cur = months[max(0, i - span + 1): i + 1]
    start = i - span + 1
    prior = months[start - span: start] if start - span >= 0 else []

    def entries(p, ms):
        return [p["months"][m] for m in ms if p["months"].get(m)]

    products, no_prior = [], []
    by_cat: dict[str, list] = {}
    for p in rows:
        now, before = entries(p, cur), entries(p, prior)
        if not now and not before:
            continue
        t, b = _month_totals(now), _month_totals(before)
        by_cat.setdefault(p["category"], []).extend(now)
        growth = logic.growth(t["sales"], b["sales"]) if prior else None
        if prior and (t["sales"] or 0) > 0 and not b["sales"]:
            no_prior.append(p["product"])
        products.append({"key": p["key"], "product": p["product"], "category": p["category"], **t,
                         "prior_sales": b["sales"], "growth": growth})
    totals = _month_totals([e for p in rows for e in entries(p, cur)])
    totals["prior"] = _month_totals([e for p in rows for e in entries(p, prior)])
    categories = []
    for name, ents in by_cat.items():
        c = _month_totals(ents)
        c.update(category=name, share=logic.pct(c["sales"], totals["sales"]),
                 net_share=logic.pct(c["net"], totals["net"]),
                 kg_share=logic.pct(c["kg"], totals["kg"]))
        categories.append(c)
    categories.sort(key=lambda c: -(c["sales"] or 0))
    products.sort(key=lambda r: -(r["sales"] or 0))
    return {"end": period_end, "span": span, "months": cur, "prior_months": prior, "totals": totals,
            "categories": categories, "products": products, "no_prior": sorted(no_prior),
            "sp_only": any(totals_by_month[m]["sb"] != "full" for m in cur)}
```

`_payload(base, brand, category, sb_days, today, period_end=None, span=1)` adds, before the return:

```python
        "period": _period(rows, months, complete, mtd, period_end, span, totals),
        "period_choices": list(reversed(complete + ([mtd] if mtd else []))),
```

`build(..., period_end: str | None = None, span: int = 1)` passes both to `_payload`. The route:

```python
async def analytics(brand: str | None = None, category: str | None = None, include_inactive: bool = False,
                    period_end: str | None = None, span: int = 1, ...):
    return await service.build(db, brand=brand, category=category or None, include_inactive=include_inactive,
                               period_end=period_end, span=span)
```

- [ ] **Step 4:** `venv/Scripts/python -m pytest -q tests/test_analytics.py` → all pass.
- [ ] **Step 5:** commit `feat(analytics): sum a picked period and the period before it`.

### Task 2: The page — sections, period picker, fifteen charts

**Files:**
- Modify: `templates/portfolio_analytics.html` (rewrite markup below the tiles and the chart JS)
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `data.period`, `data.period_choices`, `data.totals`, `data.categories`, `data.products`
  (`series`), `data.category_names`, `data.months`, `data.complete_months`, `data.mtd`.
- Produces (pure page functions tested under Node):
  - `trendMonths() -> [month]`: the 12 complete months ending at `data.period.end` (+ the month to date
    when it is the end);
  - `squarify(items, x, y, w, h) -> [{item, x, y, w, h}]`: treemap layout, areas proportional to
    `item.value`, every box inside the rectangle;
  - `heatColour(g) -> css string`: `color-mix(in srgb, var(--green|--red) N%, var(--surface))`,
    N = min(80, |g| x 200), `var(--surface2)` for null;
  - `movers(products, n) -> {up: [...], down: [...]}`: by `sales - prior_sales`, n each, prior
    required.

- [ ] **Step 1: failing tests** — replace the four 19-Oct layout tests
  (`test_only_the_month_on_month_chart_is_left...`, `test_the_focus_reads...`,
  `test_the_month_table_is_newest_first...`, `test_a_change_is_coloured...`) and `FOCUS_FNS`/`_page_js`:

```python
def test_the_page_is_charts_only_in_five_sections():
    for sec in ("overview", "categories", "products", "price", "traffic"):
        assert f'data-section="{sec}"' in PAGE, sec
    for el in ('id="period-end"', 'id="span"', 'id="show-cats"', 'id="mom-chart"', 'id="donut-chart"',
               'id="waterfall-chart"', 'id="movers-chart"', 'id="share-chart"', 'id="netcat-grid"',
               'id="heatmap"', 'id="treemap"', 'id="pareto-chart"', 'id="bubble-chart"',
               'id="perkg-chart"', 'id="admap-chart"', 'id="yoy-chart"', 'id="funnel-chart"',
               'id="buybox-chart"', 'id="product-panel"'):
        assert el in PAGE, el
    script = PAGE[PAGE.rindex("<script>"):]
    assert "<table" not in script and 'id="month-table"' not in PAGE


CHART_FNS = ["trendMonths", "squarify", "heatColour", "movers"]


def _page_js(body):
    from tests.js_harness import run_template_js
    return run_template_js(ROOT / "templates" / "portfolio_analytics.html", CHART_FNS, [], body)


def test_trend_months_are_the_twelve_ending_at_the_picked_month():
    months = [f"2025-{m:02d}" for m in range(1, 13)] + ["2026-01", "2026-02"]
    out = _page_js(f"""data = {{months: {months + ["2026-03"]}, complete_months: {months},
        mtd: {{month: "2026-03"}}, period: {{end: "2026-02"}}}}; const a = trendMonths();
        data.period.end = "2026-03"; const b = trendMonths(); emit([a, b]);""")
    assert out[0] == months[-12:]
    assert out[1] == months[-12:] + ["2026-03"]


def test_the_treemap_fills_its_box_in_proportion():
    out = _page_js("""const r = squarify([{value: 6}, {value: 3}, {value: 1}], 0, 0, 100, 50);
        emit(r.map(b => [Math.round(b.w * b.h), b.x >= 0 && b.y >= 0 && b.x + b.w <= 100.01 && b.y + b.h <= 50.01]));""")
    assert [a for a, _ in out] == [3000, 1500, 500] and all(ok for _, ok in out)


def test_heat_colour_and_movers():
    out = _page_js("""emit([heatColour(0.2), heatColour(-0.5), heatColour(null),
        movers([{product: "a", sales: 10, prior_sales: 2}, {product: "b", sales: 1, prior_sales: 9},
                {product: "c", sales: 5, prior_sales: null}], 5)]);""")
    assert "var(--green) 40%" in out[0] and "var(--red) 80%" in out[1] and out[2] == "var(--surface2)"
    assert [m["product"] for m in out[3]["up"]] == ["a"] and [m["product"] for m in out[3]["down"]] == ["b"]
```

- [ ] **Step 2:** `venv/Scripts/python -m pytest -q tests/test_analytics.py -k "charts_only or trend_months or treemap or heat"` → FAIL.

- [ ] **Step 3: implement.** Markup: controls card (unchanged) · tiles · show bar (`#show-cats` chips,
  `<select id="period-end">`, `#span` seg 1/3/6/12, `#show-label`) · section switch
  (`.seg#sections` with `data-section` buttons) · `#product-panel` (hidden; `#panel-title`,
  Close, canvases `#panel-sales`, `#panel-perkg`, `#panel-buybox`) · five `<section data-section=…>`
  holding the cards listed in the spec, each canvas in `.chart`. The pure functions:

```js
function trendMonths(){
  const end = data.period ? data.period.end : data.complete_months[data.complete_months.length - 1];
  const done = data.complete_months.filter(m => m <= end).slice(-12);
  return data.mtd && end === data.mtd.month ? done.concat([end]) : done;
}
/* Squarified treemap: rows laid along the shorter side, each row's worst aspect ratio kept low. */
function squarify(items, x, y, w, h){
  const total = items.reduce((a, it) => a + it.value, 0);
  if(!total || w <= 0 || h <= 0) return [];
  const scale = w * h / total, out = [];
  let rest = items.slice(), rx = x, ry = y, rw = w, rh = h;
  while(rest.length){
    const side = Math.min(rw, rh);
    let row = [], best = Infinity;
    for(const it of rest){
      const trial = row.concat([it]), sum = trial.reduce((a, t) => a + t.value * scale, 0);
      const worst = Math.max(...trial.map(t => Math.max(side * side * t.value * scale / (sum * sum), (sum * sum) / (side * side * t.value * scale))));
      if(worst > best) break;
      row = trial; best = worst;
    }
    const sum = row.reduce((a, t) => a + t.value * scale, 0), thick = sum / side;
    let off = 0;
    for(const it of row){
      const len = it.value * scale / thick;
      out.push(rw >= rh ? {item: it, x: rx, y: ry + off, w: thick, h: len} : {item: it, x: rx + off, y: ry, w: len, h: thick});
      off += len;
    }
    if(rw >= rh){ rx += thick; rw -= thick; } else { ry += thick; rh -= thick; }
    rest = rest.slice(row.length);
  }
  return out;
}
function heatColour(g){
  if(g == null) return "var(--surface2)";
  const n = Math.round(Math.min(80, Math.abs(g) * 200));
  return `color-mix(in srgb, var(${g >= 0 ? "--green" : "--red"}) ${n}%, var(--surface))`;
}
function movers(products, n){
  const rows = products.filter(p => p.prior_sales != null && p.sales != null)
    .map(p => ({...p, delta: p.sales - p.prior_sales}));
  return {up: rows.filter(r => r.delta > 0).sort((a, b) => b.delta - a.delta).slice(0, n),
          down: rows.filter(r => r.delta < 0).sort((a, b) => a.delta - b.delta).slice(0, n)};
}
```

  Renderers, one per chart, all through `draw(id, config)` (`maintainAspectRatio:false`,
  `interaction:{mode:"index", intersect:false}` except bubble/doughnut which use `nearest`):
  `renderMomChart` (kept, months = `trendMonths()`), `renderDonut` (doughnut of `period.categories`,
  seg Revenue / Net / Kg, click → category), `renderWaterfall` (floating bars
  `[[0,S],[S-R,S],[S-R-F,S-R],[N,S-R-F],[0,N]]`, labels Sales / Refunds / Amazon fees / Ads / Net,
  red for the three costs; note when `period.sp_only`), `renderMovers` (horizontal bars, up green,
  down red, click → product), `renderShare` (stacked line with `fill` of each category's
  `share` over `trendMonths()`, y max 100), `renderNetCat` (a small canvas per category in
  `#netcat-grid`, y from the min to max of all categories' Net %), `renderHeatmap` (CSS grid of
  `<div>`s, background `heatColour(mom)`, `title` with the figure), `renderTreemap` (absolutely
  positioned `<div>`s from `squarify`, background `heatColour(net_pct - totals.net_pct)` so
  above-average Net reads green), `renderPareto` (bars of `period.products` sales, line of cumulative
  % on y1, an 80% dashed line), `renderBubble` (x sales, y Net %, r ∝ √kg, colour by category),
  `renderPerKg` (line of `per_kg` over `trendMonths()` for the focus), `renderAdMap` (bubble x TACOS
  %, y growth %, r ∝ √sales; note naming `period.no_prior`), `renderYoy` (bars this year vs last
  year sales for `trendMonths()`; last-year months missing are null), `renderFunnel` (sessions and
  units ordered bars, conversion line on y1), `renderBuyBox` (one line per product whose `buy_box`
  fell under 0.95 in a trend month; empty → a sentence), `renderPanel` (picked product: sales bars,
  ₹/kg line, Buy Box line). Only the visible section's renderers run (`renderSection()`); switching
  section renders it. Period/span/chips call `load()` with `period_end` & `span` in the query.

- [ ] **Step 4:** pytest from Step 2 → PASS; `tests/test_analytics.py tests/test_theme.py tests/test_template_render_targets.py tests/test_local_dates.py` → PASS.
- [ ] **Step 5:** browser check of every section on local data (no console errors, each canvas has a
  chart, treemap boxes fill, heat map cells coloured, product panel opens from a bubble). Commit
  `feat(analytics): charts only, in five sections, with a period picker`.

### Task 3: Docs, suite, mutations, deploy

- [ ] Replace the 19-Oct "Screen layout" bullet in `CLAUDE.md` with the charts-only layout; update the test count.
- [ ] Add mutations to `scripts/mutate_analytics.py`: prior period not the same length; growth without a prior;
  period totals averaging rates. Run → "All N mutations caught".
- [ ] `venv/Scripts/python -m pytest -q` → all pass. Commit, push, deploy, check the page 200 and `period` in the payload.
