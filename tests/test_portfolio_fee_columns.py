"""Refunds % and Amazon fees % beside TACOS and Net %: a row reads as Amazon's own sum, in percent.

Asked for as *"add a column showing amazon fees combined except for the ads cost… 100 - 40 (amazon
fees) - 30 (ads) = net 30"*, then *"I dont want values I want %"*. Amazon's net also takes REFUNDS off,
so the row reads **100% - Refunds % - Amazon fees % - TACOS = Net %**, all of sales. That identity is
asserted on every row, since it is the reason the columns exist.
"""
import io
import sys
from pathlib import Path

import pytest

from tests.js_harness import run_portfolio_js

pytestmark = pytest.mark.regression

sys.path.insert(0, str(Path(__file__).parent))
from test_portfolio_columns_render import VOCAB  # noqa: E402


async def test_EVERY_row_adds_up_refunds_fees_tacos_and_net_to_100_percent(auth_client, db):
    """On every size AND parent row. The parent is built by `_sum_sizes`, where the refunds total
    was missing, so it is checked separately from its sizes."""
    from test_portfolio_api import _seed_snapshot

    await _seed_snapshot(db)
    data = (await auth_client.get("/portfolio")).json()
    rows = [(p["product"], p) for p in data["parents"]]
    rows += [(f"{p['product']} / {s['asin']}", s) for p in data["parents"] for s in p["sizes"]]
    checked = 0
    for label, r in rows:
        if not r["sales"]:
            assert r["refunds_pct"] is None and r["fees_pct"] is None, f"{label}: a % with no sales"
            continue
        total = r["refunds_pct"] + r["fees_pct"] + r["tacos"] + r["net_pct"]
        assert abs(total - 1.0) < 0.0005, f"{label}: the percentages add to {total:.4%}, not 100%"
        checked += 1
    assert checked and any(r["refunds_pct"] for _, r in rows), "nothing with refunds was checked"


def test_the_parent_percentages_are_recomputed_from_sums_never_averaged():
    from app.portfolio import logic

    def size(asin, sales, refunded):
        return logic.size_row(
            {"childAsin": asin, "parentAsin": "B0P",
             "sales": {"orderedProductSales": {"amount": sales},
                       "refundedProductSales": {"amount": refunded},
                       "unitsOrdered": 10, "netUnitsSold": 9}},
            {asin: {"name": "X", "weight": 1}},
        )

    big, small = size("B0A", 9000, 900), size("B0B", 1000, 500)   # 10% and 50%
    parent = logic._sum_sizes([big, small])
    assert parent["refunded"] == 1400
    assert parent["refunds_pct"] == pytest.approx(0.14)          # 1,400 of 10,000 — not the 30% average


@pytest.mark.parametrize("col, expected", [("refunds_pct", "5.3%"), ("fees_pct", "33.5%")])
def test_each_percentage_column_shows_its_own_figure(col, expected):
    """Values DIFFERENT from every neighbour, so a renderer reading the wrong field cannot pass."""
    out = run_portfolio_js(VOCAB + """
const R = Object.assign({}, ROW, {refunds_pct: 0.0534, fees_pct: 0.3351, tacos: 0.3809, net_pct: 0.2306});
layout = normaliseLayout(null, data.columns);
const part = dataCells(R).split('data-col="%s"')[1] || "";
emit(part ? part.slice(part.indexOf(">") + 1, part.indexOf("</td>")).replace(/<[^>]+>/g, "").trim() : null);
""" % col)
    assert out == expected


def test_the_totals_row_recomputes_the_percentages_from_the_rupee_sums():
    out = run_portfolio_js(VOCAB + """
const A = Object.assign({}, ROW, {sales: 9000, refunded: 900, fees_total: 2700});
const B = Object.assign({}, ROW, {sales: 1000, refunded: 500, fees_total: 300});
layout = normaliseLayout(null, data.columns);
const html = totalsRow([A, B], false);
const cellOf = id => { const p = html.split('data-col="' + id + '"')[1] || "";
  return p.slice(p.indexOf(">") + 1, p.indexOf("</td>")).replace(/<[^>]+>/g, "").trim(); };
emit([cellOf("refunds_pct"), cellOf("fees_pct")]);
""")
    assert out == ["14.0%", "30.0%"]


def test_the_columns_read_in_the_order_of_the_sum_and_no_rupee_column_was_added():
    from app.portfolio import columns as C

    order = [c["id"] for c in C.COLUMNS]
    assert order.index("refunds_pct") < order.index("fees_pct") < order.index("tacos") \
        < order.index("net_pct")
    assert not {"refunded", "fees_total", "net"} & set(order), "a rupee column is back on the screen"
    labels = {c["id"]: c["label"] for c in C.COLUMNS}
    assert labels["fees_pct"] == "Amazon fees %" and labels["refunds_pct"] == "Refunds %"


def test_every_new_column_sorts_by_itself_not_by_sales():
    out = run_portfolio_js("""
emit(["refunds_pct", "fees_pct"].filter(id => !FIELDS[COLUMN_DEFS[id].sortKey]));
""")
    assert out == []


async def test_the_workbook_carries_the_same_percentages_as_the_screen(auth_client, db):
    from openpyxl import load_workbook

    from test_portfolio_api import _seed_snapshot

    await _seed_snapshot(db)
    screen = (await auth_client.get("/portfolio")).json()
    sheet = load_workbook(io.BytesIO((await auth_client.get("/portfolio/download.xlsx")).content)).active
    rows = [[c.value for c in r] for r in sheet.iter_rows()]
    at = next(i for i, r in enumerate(rows) if r and r[0] == "Product")
    h = rows[at]
    asin, ref, fee = h.index("ASIN"), h.index("Refunds %"), h.index("Amazon fees %")
    by_asin = {r[asin]: (r[ref], r[fee]) for r in rows[at + 1:] if r[asin]}

    def pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    for s in (s for p in screen["parents"] for s in p["sizes"]):
        assert by_asin[s["asin"]] == (pct(s["refunds_pct"]), pct(s["fees_pct"])), s["asin"]
