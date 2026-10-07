from datetime import timedelta

import pytest

from app import ist
from app.repeat import repository

pytestmark = pytest.mark.regression


def _line(order, buyer, day, child="B0CHILD001", parent="B0PARENT01"):
    return {"amazon_order_id": order, "shipment_item_id": "1", "buyer_key": buyer,
            "purchase_day": day.isoformat(), "seller_sku": "s", "child_asin": child,
            "parent_asin": parent, "units": 1}


async def _seed(db, buyers=25, back=5, howrah=False):
    as_of = ist.yesterday() - timedelta(days=3)
    first = as_of - timedelta(days=59)                     # inside the 30-day window's period
    lines = []
    for i in range(buyers):
        lines.append(_line(f"a{i}", f"k{i}", first))
        if i < back:                                       # come back 10 days later
            lines.append(_line(f"b{i}", f"k{i}", first + timedelta(days=10)))
    if howrah:
        lines += [_line(f"h{i}", f"h{i}", first, child="B0HOWRAH01", parent="B0HOWRAHP1")
                  for i in range(30)]
    await repository.save_lines(db, lines)
    await repository.record_run(db, window_start=(as_of - timedelta(days=400)).isoformat(),
                                window_end=(as_of + timedelta(days=3)).isoformat(), status="done")


@pytest.fixture
def catalogue(monkeypatch):
    cat = {"B0CHILD001": {"name": "Chana Sattu", "brand": "Mithila Foods", "fba_sku": "s"},
           "B0HOWRAH01": {"name": "Bengali Posta", "brand": "Howrah Foods"}}

    async def fake():
        return cat, None, "sheet"
    monkeypatch.setattr("app.repeat.service.load_catalogue", fake)
    return cat


async def test_the_route_returns_the_30_day_same_repeat_from_counts(auth_client, db, catalogue):
    await _seed(db)
    data = (await auth_client.get("/portfolio/repeat")).json()
    row = next(r for r in data["rows"] if r["parent_asin"] == "B0PARENT01")
    assert row["product"] == "Chana Sattu" and row["w"]["30"]["buyers"] == 25
    assert row["w"]["30"]["same_pct"] == pytest.approx(5 / 25)
    assert data["total"]["30"] == {"buyers": 25, "repeat_pct": pytest.approx(5 / 25)}
    assert data["brand"] == "Mithila Foods"


async def test_a_small_cohort_returns_None_not_zero(auth_client, db, catalogue):
    await _seed(db, buyers=10)
    row = (await auth_client.get("/portfolio/repeat")).json()["rows"][0]
    assert row["w"]["30"]["buyers"] == 10 and row["w"]["30"]["same_pct"] is None


async def test_the_brand_filter_keeps_other_brands_out(auth_client, db, catalogue):
    await _seed(db, howrah=True)
    mithila = (await auth_client.get("/portfolio/repeat")).json()
    assert [r["parent_asin"] for r in mithila["rows"]] == ["B0PARENT01"]
    assert mithila["brands"] == ["Howrah Foods", "Mithila Foods"]
    howrah = (await auth_client.get("/portfolio/repeat?brand=Howrah%20Foods")).json()
    assert [r["product"] for r in howrah["rows"]] == ["Bengali Posta"]
    assert howrah["total"]["30"]["buyers"] == 30


async def test_nothing_stored_yet_says_so_instead_of_zeros(auth_client, catalogue):
    data = (await auth_client.get("/portfolio/repeat")).json()
    assert data["rows"] == [] and data["as_of"] is None


async def test_as_of_leaves_the_newest_days_for_late_shipments(auth_client, db, catalogue):
    await _seed(db)
    data = (await auth_client.get("/portfolio/repeat")).json()
    assert data["as_of"] == (ist.yesterday() - timedelta(days=3)).isoformat()


async def test_the_payload_never_carries_a_buyer_key(auth_client, db, catalogue):
    await _seed(db)
    text = (await auth_client.get("/portfolio/repeat")).text
    assert '"k1"' not in text and "buyer_key" not in text


async def test_the_route_needs_a_login(client):
    r = await client.get("/portfolio/repeat", follow_redirects=False)
    assert r.status_code in (302, 303, 401)


def _econ(child, parent, sales):
    return {"parentAsin": parent, "childAsin": child,
            "sales": {"orderedProductSales": {"amount": sales}, "unitsOrdered": 10,
                      "netUnitsSold": 10, "refundedProductSales": {"amount": 0}},
            "fees": [], "ads": [], "netProceeds": {"total": {"amount": sales / 2}}}


def test_names_match_the_profit_view_for_the_same_children():
    """The same product must read the same on both sub-tabs, including a derived family name and
    the collision suffix the Profit view adds."""
    from app.portfolio import logic as pf
    from app.repeat import logic
    catalogue = {
        "B0RC000001": {"name": "Peri Peri Roasted Chana", "brand": "Mithila Foods", "weight": 0.5},
        "B0RC000002": {"name": "Nimbu Pudina Roasted Chana", "brand": "Mithila Foods", "weight": 0.5},
        "B0RC000003": {"name": "Roasted Chana", "brand": "Mithila Foods", "weight": 1.0},
        "B0CS000001": {"name": "Chana Sattu", "brand": "Mithila Foods", "weight": 1.0},
        "B0CS000002": {"name": "Chana Sattu", "brand": "Mithila Foods", "weight": 0.5},
    }
    rows = [_econ("B0RC000001", "B0RCPAR001", 900), _econ("B0RC000002", "B0RCPAR001", 300),
            _econ("B0RC000003", "B0RCPAR002", 500), _econ("B0CS000001", "B0CSPAR001", 800),
            _econ("B0CS000002", "B0CSPAR001", 200)]
    data = pf.portfolio(rows, catalogue, {})
    profit = {p["parent_asin"]: p["product"] for p in data["parents"]}
    children = {p["parent_asin"]: [catalogue[s["asin"]]["name"] for s in p["sizes"]]
                for p in data["parents"]}
    assert logic.name_parents(children) == profit
    assert profit["B0RCPAR001"] == "Roasted Chana (2 flavours)"


async def test_a_fetch_that_includes_TODAY_still_ends_as_of_at_yesterday_minus_the_lag(
        auth_client, db, catalogue):
    await _seed(db)
    await repository.record_run(db, window_start=ist.today().isoformat(),
                                window_end=ist.today().isoformat(), status="done")
    data = (await auth_client.get("/portfolio/repeat")).json()
    assert data["as_of"] == (ist.yesterday() - timedelta(days=3)).isoformat()
