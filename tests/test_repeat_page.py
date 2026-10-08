"""The Repeat page's renderers, EXECUTED under Node rather than grepped."""
from pathlib import Path

import pytest

from tests.js_harness import run_template_js

pytestmark = pytest.mark.regression
T = Path(__file__).parent.parent / "templates" / "portfolio_repeat.html"
FUNCS = ["esc", "num", "pct", "cellPct", "winCells", "rowHtml", "totalRowHtml"]
CONSTS = ["WINS"]
DATA = """data = {min_cohort: 20, fba_partial_below: 0.6, brand: "Mithila Foods",
 windows: {"30": {available: true}, "60": {available: true},
           "90": {available: false, reason: "needs order history from 2026-03-06"}},
 total: {"30": {buyers: 1200, repeat_pct: 0.086, came_from_pct: 0.5},
         "60": {buyers: 1100, repeat_pct: 0.127}}};
const ROW = {parent_asin: "P1", product: "Jau Sattu", fba_share: 0.92,
 w: {"30": {buyers: 1404, same_pct: 0.077, came_from_pct: 0.071, went_on_pct: 0.05},
     "60": {buyers: 1300, same_pct: 0.143, came_from_pct: 0.091, went_on_pct: 0.06},
     "90": {buyers: 999, same_pct: 0.55, came_from_pct: 0.44}},
 flows: {"60": {came_from: [{product: "Chana Sattu", customers: 73}],
                went_on: [{product: "Jeera Chana Sattu", customers: 58}]}},
 basket: {orders: 900, multi_pct: 0.21, with: [{product: "Makkai Sattu", orders: 193}]}};"""


def _cells(expr):
    return dict(run_template_js(T, FUNCS, CONSTS, DATA + f"""
const h = {expr};
emit([...h.matchAll(/data-col="([^"]+)"[^>]*>([\\s\\S]*?)<\\/td>/g)]
  .map(m => [m[1], m[2].replace(/<[^>]+>/g, "").trim()]));"""))


def test_a_row_shows_each_window_in_its_own_columns():
    got = _cells("rowHtml(ROW)")
    assert got["buyers-30"] == "1,404" and got["same-30"] == "7.7%" and got["from-30"] == "7.1%"
    assert got["buyers-60"] == "1,300" and got["same-60"] == "14.3%" and got["from-60"] == "9.1%"


def test_an_unavailable_window_renders_a_dash_not_zero():
    got = _cells("rowHtml(ROW)")
    assert got["buyers-90"] == "—" and got["same-90"] == "—" and got["from-90"] == "—"


def test_the_total_row_is_the_brand_figure_and_has_no_cross_flow():
    """The fixture's total even CARRIES a came_from_pct: a brand total must never show one."""
    got = _cells("totalRowHtml()")
    assert got["product"].startswith("Mithila Foods")
    assert got["buyers-30"] == "1,200" and got["same-30"] == "8.6%" and got["from-30"] == "—"
    assert got["same-60"] == "12.7%"


def test_a_cohort_under_the_minimum_shows_a_dash_with_the_reason():
    out = run_template_js(T, FUNCS, CONSTS, DATA + """
emit(rowHtml(Object.assign({}, ROW, {w: {"30": {buyers: 12, same_pct: null, came_from_pct: null}}})));""")
    assert "only 12 buyers" in out


def test_a_low_fba_share_is_flagged_partial_and_a_high_one_is_not():
    out = run_template_js(T, FUNCS, CONSTS, DATA + """
emit([rowHtml(Object.assign({}, ROW, {fba_share: 0.4})).includes("partial"),
      rowHtml(ROW).includes("partial")]);""")
    assert out == [True, False]


def test_there_is_no_expand_panel():
    """Removed on request ("I dont want this"): a click on a row opens nothing."""
    src = T.read_text(encoding="utf-8")
    for gone in ("flowsHtml", 'class="detail"', "data-flow", "open.has("):
        assert gone not in src, gone


def test_the_headings_and_the_brand_total_stay_in_view_when_scrolling():
    src = T.read_text(encoding="utf-8")
    css = src[src.index("<style>"):src.index("</style>")]
    assert "max-height:var(--rp-cap" in css, "an uncapped wrapper never scrolls, so sticky is inert"
    assert "position:sticky;top:0" in css
    assert "thead tr.sub th{top:var(--rp-h1" in css
    assert "top:calc(var(--rp-h1, 31px) + var(--rp-h2, 31px))" in css
    assert '<tr class="sub">' in src and "sizeTable();" in src


def test_a_product_name_is_escaped():
    out = run_template_js(T, FUNCS, CONSTS, DATA + """
emit(rowHtml(Object.assign({}, ROW, {product: "<img src=x>"})));""")
    assert "<img" not in out and "&lt;img" in out


async def test_both_portfolio_pages_carry_the_switch_with_the_right_tab_lit(auth_client):
    profit = (await auth_client.get("/portfolio-page")).text
    repeat = (await auth_client.get("/portfolio-page/repeat")).text
    for html in (profit, repeat):
        assert 'href="/portfolio-page/repeat"' in html and 'href="/portfolio-page"' in html
    assert 'class="seg-btn on" href="/portfolio-page"' in profit
    assert 'class="seg-btn on" href="/portfolio-page/repeat"' in repeat


def test_the_page_never_mentions_a_buyer_key():
    assert "buyer_key" not in T.read_text(encoding="utf-8")


def test_the_page_sizes_its_icons():
    """Found in the browser: without the `.ico` rule the sprite's refresh icon filled the card."""
    assert ".ico{width:1em;height:1em" in T.read_text(encoding="utf-8")


# ── sorting ─────────────────────────────────────────────────────────────────────────────────────
# Asked for as "make it sortable from each column like we did in the portfolio. top to bottom,
# bottom to top for each column". Executed, not grepped: the order IS the behaviour.

SORT_DATA = DATA + """
data.rows = [
 {parent_asin: "A", product: "Jau Sattu", fba_share: 0.9,
  w: {"30": {buyers: 50, same_pct: 0.10, came_from_pct: 0.02}, "60": {buyers: 80, same_pct: 0.20}}},
 {parent_asin: "B", product: "Chana Sattu", fba_share: 0.5,
  w: {"30": {buyers: 900, same_pct: null, came_from_pct: 0.09}, "60": {buyers: 70, same_pct: 0.05}}},
 {parent_asin: "C", product: "Ragi Atta", fba_share: null,
  w: {"30": {buyers: 300, same_pct: 0.30, came_from_pct: 0.01}, "60": {buyers: 90, same_pct: 0.12}}}];"""


def _order(key, direction):
    return run_template_js(T, ["sortValue", "sortedRows"], [], SORT_DATA + f"""
sort = {{key: {key!r}, dir: {direction}}};
emit(sortedRows().map(r => r.parent_asin));""")


@pytest.mark.parametrize("key, desc", [
    ("buyers-30", ["B", "C", "A"]), ("buyers-60", ["C", "A", "B"]),
    ("from-30", ["B", "A", "C"]), ("same-60", ["A", "C", "B"])])
def test_every_window_column_sorts_both_ways(key, desc):
    assert _order(key, -1) == desc
    assert _order(key, 1) == desc[::-1]


def test_product_sorts_by_name():
    assert _order("product", 1) == ["B", "A", "C"]          # Chana, Jau, Ragi
    assert _order("product", -1) == ["C", "A", "B"]


def test_a_dash_sorts_LAST_in_both_directions():
    """Too few buyers is not 0%: ranking it lowest would mislabel the least-known product."""
    assert _order("same-30", -1)[-1] == "B" and _order("same-30", 1)[-1] == "B"
    assert _order("fba", -1) == ["A", "B", "C"] and _order("fba", 1) == ["B", "A", "C"]


def test_an_unavailable_window_sorts_as_all_dashes_not_by_hidden_numbers():
    """The 90-day window is unavailable in DATA; its stored numbers must not order the rows."""
    assert _order("same-90", -1) == ["B", "A", "C"]          # every value null -> by name


def test_every_column_header_is_a_sort_control():
    src = T.read_text(encoding="utf-8")
    body = src[src.index("function render(){"):src.index("async function load(")]
    for key in ('"product"', '"fba"', "`buyers-${n}`", "`same-${n}`", "`from-${n}`"):
        assert f"th({key}" in body, f"{key} has no sort control"
    assert 'tabindex="0" role="button" aria-sort' in src
    assert "sortedRows().map(" in body, "the body must render in the sorted order"
    assert 'addEventListener("keydown"' in src
