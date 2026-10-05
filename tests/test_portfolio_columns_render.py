"""The Portfolio table's column alignment, proven by EXECUTING the page's render functions.

A table where TACOS and ACOS have swapped cells has the right COUNT of columns and the wrong figure
under each heading — so every cell carries `data-col`, and these tests compare ids, in order.
"""
import pytest

from tests.js_harness import run_portfolio_js

pytestmark = pytest.mark.regression


def test_the_harness_can_run_the_page_code():
    assert run_portfolio_js("emit(normaliseLayout(null, data.columns).order.length)") == 13


VOCAB = """
data.columns = [
  {id:"product",label:"Product",locked:true},{id:"verdict",label:"Verdict",locked:false},
  {id:"sales",label:"Sales",locked:true},{id:"ad_spend",label:"Ad spend",locked:true},
  {id:"tacos",label:"TACOS",locked:false},{id:"acos",label:"ACOS",locked:false},
  {id:"net_pct",label:"Net %",locked:false},
  {id:"units_ordered",label:"Units ordered",locked:true},{id:"units",label:"Net units",locked:true},
  {id:"weight_ordered_kg",label:"Weight ordered",locked:true},
  {id:"weight_kg",label:"Weight",locked:true},{id:"returns_pct",label:"Returns",locked:false},
  {id:"rating",label:"Rating",locked:false},{id:"decision",label:"Decision",locked:false}];
data.group_flags = {};
const ROW = {product:"Chana Sattu", verdict:"SCALE", sales:1000, ad_spend:200, tacos:0.2,
  acos:0.5, net:300, net_pct:0.3, units:10, weight_kg:5, returns_pct:0.01, rating:4.2,
  rating_count:40, decision:"keep", asin:"B0X", parent_asin:"B0P", units_ordered:10,
  units_refunded:0, ads_cost:200, ad_attributed_sales:400, size:"1 kg", label:"1 kg"};
const ids = html => [...html.matchAll(/<t[hd][^>]*data-col="([^"]+)"/g)].map(m => m[1]);
const span = html => [...html.matchAll(/<td([^>]*)>/g)]
  .reduce((a, m) => a + (+((/colspan="(\d+)"/.exec(m[1]) || [0, 1])[1])), 0);
"""

LAYOUTS = {
    "default": "null",
    "shuffled_three_hidden":
        '{order:["decision","net_pct","sales","rating","verdict","units","acos","weight_kg",'
        '"ad_spend","returns_pct","tacos"], hidden:["acos","verdict","returns_pct"]}',
    "all_shown": '{order:[], hidden:[]}',
    "stale_id": '{order:["gone","rating","sales"], hidden:["gone"]}',
}


@pytest.mark.parametrize("name", LAYOUTS)
def test_header_product_size_flavour_and_totals_rows_list_the_SAME_columns_in_the_SAME_order(name):
    out = run_portfolio_js(VOCAB + f"""
layout = normaliseLayout({LAYOUTS[name]}, data.columns);
const head = ids(headerHtml());
emit({{head,
  product: ids(`<td data-col="product"></td>` + dataCells(ROW)),
  size: ids(sizeRowHtml(ROW, false)),
  flavour: ids(`<td data-col="product"></td>` + detailCells(ROW)),
  totals: ids(totalsRow([ROW], false)),
  sizeSpan: span(sizeRowHtml(ROW, false)), headCount: head.length}});
""")
    assert out["head"][0] == "product"
    for row in ("product", "size", "flavour", "totals"):
        assert out[row] == out["head"], f"{name}: the {row} row is out of line with the header"
    assert out["sizeSpan"] == out["headCount"]


def test_hidden_columns_are_absent_and_the_locked_ones_cannot_be_hidden():
    out = run_portfolio_js(VOCAB + """
layout = normaliseLayout({order:[], hidden:["sales","units","acos","decision"]}, data.columns);
emit(ids(headerHtml()));
""")
    assert "acos" not in out and "decision" not in out
    assert {"sales", "units", "ad_spend", "weight_kg"} <= set(out)


def test_the_size_rows_rating_cell_no_longer_spans_into_decision():
    """It spanned Rating AND Decision; once those can be separated, a span would put a cell under
    the wrong heading."""
    out = run_portfolio_js(VOCAB + """
layout = normaliseLayout({order:["rating","sales","decision"], hidden:[]}, data.columns);
emit(sizeRowHtml(ROW, false));
""")
    assert "colspan" not in out


def test_min_width_follows_the_visible_columns():
    out = run_portfolio_js(VOCAB + """
layout = normaliseLayout({order:[], hidden:[]}, data.columns); const all = tableMinWidth();
layout = normaliseLayout({order:[], hidden:["verdict","tacos","acos","net_pct","rating"]},
                         data.columns);
emit([all, tableMinWidth()]);
""")
    assert out[0] > out[1] > 0


def test_numeric_columns_render_as_num_cells():
    """Tabular figures (theme.css) reach only `td.num`; a money column losing the class goes
    proportional on the warehouse tablet and nowhere it would be reviewed."""
    out = run_portfolio_js(VOCAB + """
layout = normaliseLayout({order:[], hidden:[]}, data.columns);
const html = dataCells(ROW);
emit([...html.matchAll(/<td data-col="([^"]+)"( class="([^"]*)")?/g)]
  .map(m => [m[1], (m[3] || "").split(" ").includes("num")]));
""")
    num = {col: is_num for col, is_num in out}
    for col in ("sales", "ad_spend", "tacos", "acos", "net_pct", "units", "weight_kg", "returns_pct"):
        assert num[col], f"{col} is not a num cell"
    assert not num["verdict"] and not num["rating"] and not num["decision"]


@pytest.mark.parametrize("saved", [
    None, {"order": ["gone", "rating", "rating"], "hidden": ["sales", "acos"]},
    {"order": ["decision"], "hidden": []}, {"order": "x"},
])
def test_the_client_normaliser_agrees_with_the_server(saved):
    """Two normalisers exist — the server's, and the client's twin for browser-scope and optimistic
    re-render. Two copies of a rule drift; this pins them to the same answers."""
    import json

    from app.portfolio import columns as C

    client = run_portfolio_js(VOCAB + f"emit(normaliseLayout({json.dumps(saved)}, data.columns));")
    assert client == C.normalise_column_layout(saved)


PANEL = VOCAB + """
layout = normaliseLayout(null, data.columns);
let saved = []; function saveLayout(){ saved.push(JSON.parse(JSON.stringify(layout))); }
function render(){} function remember(){}
"""


def test_the_panel_lists_every_movable_column_and_disables_the_locked_ticks():
    import re

    html = run_portfolio_js(PANEL + "emit(columnsPanelHtml());", panel=True)
    for col in ("verdict", "sales", "tacos", "rating", "decision"):
        assert f'data-col-row="{col}"' in html
    assert 'data-col-row="product"' not in html, "Product is pinned and is not listed as movable"
    sales = re.search(r'data-col-row="sales".*?</li>', html, re.S).group(0)
    assert "disabled" in sales, "a protected column's tick box must be disabled"
    tacos = re.search(r'data-col-row="tacos".*?</li>', html, re.S).group(0)
    tick = re.search(r'<input type="checkbox"[^>]*>', tacos).group(0)
    assert "disabled" not in tick, "a hideable column's tick box is disabled"


def test_moving_a_column_changes_the_order_and_saves():
    out = run_portfolio_js(PANEL + """
moveColumn("decision", -1);
emit({order: layout.order, saves: saved.length});
""", panel=True)
    assert out["order"].index("decision") == out["order"].index("rating") - 1
    assert out["saves"] == 1


def test_moving_past_either_end_is_a_no_op():
    """Starts from a NON-default layout on purpose. From the default, a broken move that corrupts the
    order is normalised straight back to the default — i.e. to where it started — and looks like a
    no-op. The mutation harness caught this test passing against exactly that bug."""
    out = run_portfolio_js(PANEL + """
layout = normaliseLayout({order: ["decision", "rating", "sales", "verdict"], hidden: ["acos"]},
                         data.columns);
const before = JSON.stringify(layout);
const first = layout.order[0], last = layout.order[layout.order.length - 1];
moveColumn(first, -1); moveColumn(last, +1);
emit({same: JSON.stringify(layout) === before, saves: saved.length});
""", panel=True)
    assert out["same"], "moving past an end changed the layout"
    assert out["saves"] == 0


def test_hiding_the_sorted_column_resets_the_sort_to_sales():
    out = run_portfolio_js(PANEL + """
sort = {key: "tacos", dir: 1};
setHidden("tacos", true);
emit({sort, hidden: layout.hidden});
""", panel=True)
    assert out["sort"] == {"key": "sales", "dir": -1}
    assert "tacos" in out["hidden"]


def test_hiding_an_UNSORTED_column_leaves_the_sort_alone():
    out = run_portfolio_js(PANEL + """
sort = {key: "net_pct", dir: 1};
setHidden("tacos", true);
emit(sort);
""", panel=True)
    assert out == {"key": "net_pct", "dir": 1}


def test_a_locked_column_cannot_be_hidden_even_by_calling_the_handler():
    out = run_portfolio_js(PANEL + 'setHidden("sales", true); emit({h: layout.hidden, s: saved.length});',
                           panel=True)
    assert "sales" not in out["h"]
    assert out["s"] == 0, "a refused change must not trigger a save"


def test_reset_restores_todays_layout():
    out = run_portfolio_js(PANEL + """
moveColumn("decision", -1); setHidden("acos", true);
applyLayout(null);
emit(layout);
""", panel=True)
    from app.portfolio import columns as C
    assert out == C.DEFAULT_LAYOUT


def test_the_panel_flips_to_open_rightward_when_it_would_start_off_screen():
    """Found in a browser at 375px: the button wraps to the LEFT edge on a phone, and the panel is
    anchored by its right edge, so it opened at x=-172 with half its rows unreachable.

    Source-level, because Node has no layout engine to measure a rect in — the property is that the
    handler MEASURES and flips, and that the flip class and a viewport cap both exist in the CSS.
    """
    from pathlib import Path
    import re
    text = Path("templates/portfolio.html").read_text(encoding="utf-8")
    handler = text[text.index('$("cols-btn").addEventListener("click"'):]
    handler = handler[:handler.index("\n});")]
    assert 'classList.remove("from-left")' in handler, "must re-measure from the default anchor"
    assert re.search(r'getBoundingClientRect\(\)\.left\s*<\s*\d+\)\s*panel\.classList\.add\("from-left"\)',
                     handler)
    assert re.search(r"\.cols-panel\.from-left\{left:0;right:auto\}", text)
    assert "max-width:calc(100vw - 16px)" in text
