"""The Portfolio table's column alignment, proven by EXECUTING the page's render functions.

A table where TACOS and ACOS have swapped cells has the right COUNT of columns and the wrong figure
under each heading — so every cell carries `data-col`, and these tests compare ids, in order.
"""
import pytest

from tests.js_harness import run_portfolio_js

pytestmark = pytest.mark.regression


def test_the_harness_can_run_the_page_code():
    assert run_portfolio_js("emit(normaliseLayout(null, data.columns).order.length)") == 11


VOCAB = """
data.columns = [
  {id:"product",label:"Product",locked:true},{id:"verdict",label:"Verdict",locked:false},
  {id:"sales",label:"Sales",locked:true},{id:"ad_spend",label:"Ad spend",locked:true},
  {id:"tacos",label:"TACOS",locked:false},{id:"acos",label:"ACOS",locked:false},
  {id:"net_pct",label:"Net %",locked:false},{id:"units",label:"Units",locked:true},
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
