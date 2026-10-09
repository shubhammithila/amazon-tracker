"""Net ₹/kg: what a kilogram earns after refunds, every Amazon fee and ads, before product cost.

Asked for as *"net value… x rs/kg… what per kg value I am getting after all deductions… link it to my
purchase later to get the exact profit… the same for the underlying weight SKUs"*.
"""
import json

import pytest

from app.portfolio import columns, export, logic

pytestmark = pytest.mark.regression

CAT = {"B0BIG00001": {"name": "Chana Sattu", "brand": "Mithila Foods", "weight": 1.0},
       "B0SMALL001": {"name": "Chana Sattu", "brand": "Mithila Foods", "weight": 0.5},
       "B0NOWT0001": {"name": "Chana Sattu", "brand": "Mithila Foods"}}


def _row(child, units, sales, net):
    return {"parentAsin": "B0PARENT01", "childAsin": child,
            "sales": {"orderedProductSales": {"amount": sales}, "unitsOrdered": units,
                      "netUnitsSold": units, "refundedProductSales": {"amount": 0}},
            "fees": [], "ads": [], "netProceeds": {"total": {"amount": net}}}


def _parent(rows):
    return logic.portfolio(rows, CAT, {}, {})["parents"][0]


def test_a_size_earns_its_net_over_its_net_weight():
    p = _parent([_row("B0BIG00001", 400, 40000, 10000), _row("B0SMALL001", 10, 1500, 450)])
    by = {s["asin"]: s for s in p["sizes"]}
    assert by["B0BIG00001"]["net_per_kg"] == 25.0         # ₹10,000 over 400 kg
    assert by["B0SMALL001"]["net_per_kg"] == 90.0         # ₹450 over 10 × 0.5 kg


def test_the_parent_divides_the_SUMS_never_averages_the_sizes():
    """(10,000 + 450) / (400 + 5) = ₹25.80/kg. The mean of 25 and 90 would be ₹57.50."""
    p = _parent([_row("B0BIG00001", 400, 40000, 10000), _row("B0SMALL001", 10, 1500, 450)])
    assert p["net_per_kg"] == pytest.approx(10450 / 405, abs=0.01)
    assert p["net_per_kg"] != pytest.approx(57.5, abs=1)


def test_a_size_with_no_pack_weight_is_left_out_of_BOTH_sides():
    """Its rupees cannot be per kg of anything, so they leave the numerator as its kilos leave the
    denominator; otherwise the rate is overstated by exactly that size's net."""
    p = _parent([_row("B0BIG00001", 400, 40000, 10000), _row("B0NOWT0001", 50, 9000, 5000)])
    assert p["net_per_kg"] == 25.0
    nowt = next(s for s in p["sizes"] if s["asin"] == "B0NOWT0001")
    assert nowt["net_per_kg"] is None


def test_a_loss_reads_as_a_negative_rate_and_no_units_as_a_dash():
    p = _parent([_row("B0BIG00001", 20, 2000, -600), _row("B0SMALL001", 0, 0, -50)])
    by = {s["asin"]: s for s in p["sizes"]}
    assert by["B0BIG00001"]["net_per_kg"] == -30.0
    assert by["B0SMALL001"]["net_per_kg"] is None


def test_the_totals_row_agrees_with_the_pages_own_javascript():
    from tests.js_harness import run_portfolio_js
    rows = logic.portfolio([_row("B0BIG00001", 400, 40000, 10000), _row("B0SMALL001", 10, 1500, 450),
                            _row("B0NOWT0001", 50, 9000, 5000)], CAT, {}, {})["skus"]
    js = run_portfolio_js(f"const t = computeTotals({json.dumps(rows)}); emit(t.netWeighed / t.weight);")
    assert export.totals(rows)["net_per_kg"] == pytest.approx(js) == pytest.approx(10450 / 405)


def test_it_sits_beside_net_pct_on_screen_and_in_the_file():
    ids = [c["id"] for c in columns.COLUMNS]
    assert ids.index("net_per_kg") == ids.index("net_pct") + 1
    assert export.COLUMN_KINDS["net_per_kg"] == "per_kg"
    assert export.display(25.8, "per_kg") == "₹25.80"
    assert export.display(1234.5, "per_kg") == "₹1,234.50"
    assert export.display(-30, "per_kg") == "-₹30.00"


def test_the_totals_CELL_on_screen_shows_the_like_for_like_rate():
    """The column's own renderer, not just computeTotals: ₹10,450 over 405 kg = ₹25.80."""
    from tests.js_harness import run_portfolio_js
    rows = logic.portfolio([_row("B0BIG00001", 400, 40000, 10000), _row("B0SMALL001", 10, 1500, 450),
                            _row("B0NOWT0001", 50, 9000, 5000)], CAT, {}, {})["skus"]
    cell = run_portfolio_js(
        f"emit(COLUMN_DEFS.net_per_kg.total(computeTotals({json.dumps(rows)})));")
    assert "₹25.80" in cell
