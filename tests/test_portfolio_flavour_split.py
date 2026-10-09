"""A multi-flavour parent is one row PER FLAVOUR, on the Profit view as on Repeat customers.

Asked for as *"for the roasted chana flavours… make it separate… in the profit tab also. data
should corroborate. each flavour has three sku's"*.
"""
import pytest

from app.portfolio import export, logic

pytestmark = pytest.mark.regression

CAT = {f"B0{f}{s}": {"name": f"{name} Roasted Chana", "brand": "Mithila Foods", "weight": w}
       for f, name in (("PERI", "Peri Peri"), ("HING", "Hing Jeera"))
       for s, w in (("0025", 0.25), ("0050", 0.5), ("0100", 1.0))}
KEYS = {a: min(x for x in CAT if x[:6] == a[:6]) for a in CAT}


def _row(child, sales):
    return {"parentAsin": "B0RCPARENT", "childAsin": child,
            "sales": {"orderedProductSales": {"amount": sales}, "unitsOrdered": 10,
                      "netUnitsSold": 10, "refundedProductSales": {"amount": 0}},
            "fees": [], "ads": [], "netProceeds": {"total": {"amount": sales / 3}}}


def _data(keys=KEYS):
    rows = [_row(a, 100 * (i + 1)) for i, a in enumerate(sorted(CAT))]
    ratings = {a: {"rating": 4.2, "rating_count": 477, "scraped_at": "2026-10-01"} for a in CAT}
    return logic.portfolio(rows, CAT, ratings, {}, flavour_keys=keys)


def test_each_flavour_is_its_own_row_holding_its_three_sizes():
    parents = {p["product"]: p for p in _data()["parents"]}
    assert set(parents) == {"Peri Peri Roasted Chana", "Hing Jeera Roasted Chana"}
    for p in parents.values():
        assert len(p["sizes"]) == 3 and p["flavour_groups"] == []
        assert p["family_asin"] == "B0RCPARENT" and p["parent_asin"] in KEYS.values()


def test_the_split_rows_add_up_to_the_unsplit_parent():
    split = _data()
    whole = _data(keys={})
    assert len(whole["parents"]) == 1
    assert sum(p["sales"] for p in split["parents"]) == pytest.approx(whole["parents"][0]["sales"])
    assert split["totals"]["sales"] == pytest.approx(whole["totals"]["sales"])


def test_split_flavours_share_ONE_review_pool_in_the_totals():
    """Amazon pools reviews per family: two flavour rows must not count 477 reviews twice."""
    t = export.totals(_data()["parents"])
    assert t["reviews"] == 477


def test_the_page_dedupes_reviews_on_the_family_too():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "templates" / "portfolio.html").read_text(encoding="utf-8")
    assert "const family = r.family_asin || r.parent_asin || r.asin;" in src
