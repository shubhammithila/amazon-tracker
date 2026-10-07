"""The Repeat page's renderers, EXECUTED under Node rather than grepped."""
from pathlib import Path

import pytest

from tests.js_harness import run_template_js

pytestmark = pytest.mark.regression
T = Path(__file__).parent.parent / "templates" / "portfolio_repeat.html"
FUNCS = ["esc", "num", "pct", "cellPct", "winCells", "rowHtml", "totalRowHtml", "flowsHtml"]
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


def test_the_expand_panel_names_sources_destinations_and_basket():
    out = run_template_js(T, FUNCS, CONSTS, DATA + 'emit(flowsHtml(ROW, "60"));')
    assert "Came from" in out and "Chana Sattu <b>73</b>" in out
    assert "Went on to" in out and "Jeera Chana Sattu <b>58</b>" in out
    assert "Bought together" in out and "Makkai Sattu <b>193</b>" in out and "21.0%" in out


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
