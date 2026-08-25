"""The Orders routes: the picking sheet, the order list, the export, and access.

Every route here reads LOCAL rows. `getOrders` allows one request every 22 seconds, so a
route that called Amazon would hang the page and 429 the moment two people opened the tab —
which is why `POST /orders/refresh` starts a background job and returns immediately.
"""
from datetime import datetime, timedelta

import pytest

from app.orders import logic, refresh, repository

pytestmark = pytest.mark.regression

#: An ASIN this file's own catalogue stub knows.
KNOWN_ASIN = "B0CWGXYLT6"


@pytest.fixture(autouse=True)
def stub_catalogue(monkeypatch):
    """A two-product MRP catalogue for these tests.

    The suite's autouse `no_live_product_sheet` returns an EMPTY catalogue so nothing hits
    the real Google Sheet — right for the shipment tests, but it means no ASIN resolves to
    a product here, and a picking sheet with no product names proves nothing. So the
    products these tests need are supplied explicitly rather than depending on whatever
    the live sheet holds today.
    """
    async def _catalogue():
        return (
            {
                KNOWN_ASIN: {"asin": KNOWN_ASIN, "name": "Chana Sattu", "weight": 0.5,
                             "brand": "Mithila Foods", "active": True},
                "B0RAGI1KG0": {"asin": "B0RAGI1KG0", "name": "Ragi Atta", "weight": 1.0,
                               "brand": "Mithila Foods", "active": True},
            },
            None,
            "sheet",
        )

    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _catalogue, raising=True)
    return _catalogue


def _ship_by_for(ist_date):
    """The UTC instant Amazon would send for "end of `ist_date` in India".

    23:59 IST is 18:29Z on the SAME calendar day, because the offset is +5:30 and 23:59
    minus 5:30 is 18:29 — still the same date. Written as a helper so no test builds it
    from the local clock by accident.
    """
    return datetime(ist_date.year, ist_date.month, ist_date.day, 18, 29)


def _row(order_id, **overrides):
    row = {
        "amazon_order_id": order_id,
        "purchase_date_utc": datetime.utcnow() - timedelta(hours=6),
        # 18:29Z on the UTC day whose IST evening is TODAY. Derived from the IST date,
        # not from utcnow(): between 18:30Z and midnight UTC the IST date is already
        # tomorrow, so building this from the UTC clock puts the deadline a day out — the
        # exact confusion this feature exists to prevent, and it failed here first.
        "latest_ship_date_utc": _ship_by_for(datetime.now(logic.IST).date()),
        "status": "Unshipped",
        "easyship_status": "PendingSchedule",
        "ship_service_level": "Std IN EZ National COD",
        "order_total": 319.0,
        "currency": "INR",
        "items_ordered": 1,
        "items_shipped": 0,
        "is_prime": False,
        "is_cod": True,
        "city": "NAVSARI",
        "state": "GUJARAT",
        "postal_code": "396445",
    }
    row.update(overrides)
    return row


async def _seed(db, orders=None, items=None):
    await repository.upsert_orders(db, orders or [_row("403-1")])
    for order_id, rows in (items or {"403-1": [
        {"asin": KNOWN_ASIN, "seller_sku": "cs-500", "title": "Chana Sattu",
         "quantity_ordered": 2}
    ]}).items():
        await repository.replace_items(db, order_id, rows)


async def test_the_orders_route_returns_the_sheet_and_the_list(auth_client, db):
    """One payload feeds both views, so they cannot disagree about a quantity."""
    await _seed(db)
    body = (await auth_client.get("/orders")).json()

    assert body["total_orders"] == 1
    assert "sheet" in body and "sections" in body["sheet"]
    # All three sections are present even when empty: a section that vanished would read
    # as a bug rather than an empty queue.
    for bucket in (logic.BUCKET_TODAY, logic.BUCKET_PICKUP, logic.BUCKET_LATER):
        assert bucket in body["sheet"]["sections"], f"{bucket} missing from the sheet"
    assert body["section_labels"][logic.BUCKET_TODAY]
    assert body["last_refreshed_at"], "the banner has no timestamp to show"


async def test_the_sheet_aggregates_the_seeded_order(auth_client, db):
    """The end-to-end shape the warehouse reads: product, size, units, orders, kg."""
    await _seed(db)
    body = (await auth_client.get("/orders")).json()
    today = body["sheet"]["sections"][logic.BUCKET_TODAY]

    assert today["totals"]["orders"] == 1
    assert today["totals"]["quantity"] == 2
    line = today["lines"][0]
    assert line["quantity"] == 2
    assert line["weight_label"], "the pack size did not resolve from the catalogue"
    assert line["kg"] and line["kg"] > 0, "no net weight on a known product"


async def test_a_ship_by_deadline_is_rendered_in_ist_not_utc(auth_client, db):
    """18:29Z must reach the client as 23:59 IST.

    The single most consequential rendering decision on this screen: UTC would show every
    deadline 5.5 hours early on the page whose only job is "what must go out today".
    """
    await _seed(db)
    body = (await auth_client.get("/orders")).json()
    order = body["orders"][0]
    assert order["ship_by_ist"], "no ship-by date reached the client"
    # ship_by_ist is an IST calendar DATE; the same instant in UTC could be the day before.
    assert order["ship_by_ist"] == datetime.now(logic.IST).date().isoformat()


async def test_the_1995_sentinel_is_not_sent_as_a_date(auth_client, db):
    """Amazon's "no deadline" value must not reach the client as a real date.

    Rendered, it reads as 31 years overdue and sorts to the top of the packer's list.
    """
    await _seed(db, orders=[_row("403-odd",
                                 latest_ship_date_utc=datetime(1995, 1, 1),
                                 ship_service_level="Std IN EZ National COD")],
                items={"403-odd": [{"asin": KNOWN_ASIN, "quantity_ordered": 1}]})
    body = (await auth_client.get("/orders")).json()
    order = next(o for o in body["orders"] if o["amazon_order_id"] == "403-odd")
    assert order["ship_by_ist"] is None, "the 1995 sentinel was sent as a date"
    assert order["overdue"] is False, "the sentinel was treated as overdue"


async def test_the_picking_sheet_downloads_as_excel(auth_client, db):
    """The printed sheet comes from the same aggregation as the screen."""
    await _seed(db)
    r = await auth_client.get("/orders/download/picking-sheet.xlsx?bucket=to_pack")
    assert r.status_code == 200, r.text
    assert "spreadsheet" in r.headers["content-type"]
    assert len(r.content) > 1000, "the workbook is suspiciously small"


async def test_an_unknown_section_is_refused(auth_client, db):
    """A typo'd bucket must not silently export the wrong section."""
    await _seed(db)
    r = await auth_client.get("/orders/download/picking-sheet.xlsx?bucket=nonsense")
    assert r.status_code == 400
    assert "nonsense" in r.json()["error"]


async def test_a_second_refresh_is_refused_over_http(auth_client, db, monkeypatch):
    """409 rather than a second job: two refreshes would 429 each other.

    The state is set directly here because this asserts the ROUTE's guard; the job's own
    guard is covered in tests/test_orders_refresh.py.
    """
    refresh.reset_state()
    refresh.STATE["running"] = True
    try:
        r = await auth_client.post("/orders/refresh")
        assert r.status_code == 409
        assert "already running" in r.json()["error"].lower()
    finally:
        refresh.reset_state()


async def test_refresh_status_is_readable_without_starting_one(auth_client):
    """The banner polls this every few seconds, so it must be cheap and side-effect free."""
    refresh.reset_state()
    body = (await auth_client.get("/orders/refresh-status")).json()
    assert body["running"] is False
    assert body["phase"] == "idle"


async def test_the_orders_area_is_required(ops_client):
    """Deny by default. An ops user without the `orders` area sees none of it.

    The area exists so the WAREHOUSE can be granted the picking sheet deliberately —
    the default must still be no access, or granting means nothing.
    """
    for method, path in (
        ("get", "/orders"),
        ("get", "/orders/refresh-status"),
        ("post", "/orders/refresh"),
        ("get", "/orders/download/picking-sheet.xlsx"),
        ("get", "/orders-page"),
    ):
        r = await getattr(ops_client, method)(path)
        assert r.status_code in (302, 303, 401, 403), f"{path} -> {r.status_code}"


def test_the_orders_area_is_grantable_and_denied_by_default():
    """It must be a real area, or the Users screen cannot offer it."""
    from app import permissions

    assert permissions.ORDERS in permissions.AREA_KEYS
    assert any(key == permissions.ORDERS for key, _l, _h in permissions.AREAS), (
        "the area is not in AREAS, so it cannot be ticked on the Users screen"
    )
    # Deny by default: an empty grant must not include it.
    assert not permissions.has("", permissions.ORDERS)
    assert permissions.has(permissions.ORDERS, permissions.ORDERS)


# ─── The screen ──────────────────────────────────────────────────────────────

def _orders_source() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "templates" / "orders.html"
    ).read_text(encoding="utf-8")


def test_the_ship_by_date_is_not_rendered_through_a_time_formatter():
    """A date put through a time formatter lands on the WRONG DAY.

    `ship_by_ist` is a calendar date ("2026-08-25") because a ship-by deadline is a day.
    The template first rendered it with `timeIST()`, whose `new Date("2026-08-25")` is
    parsed as UTC midnight — printing "26 Aug, 05:30", five and a half hours into the
    following day, in the column the warehouse plans against.

    Found in a browser, not by a test: the server was correct and the formatter was
    correct, and only their combination was wrong. So this asserts the pairing.
    """
    source = _orders_source()
    assert "esc(dateIST(o.ship_by_ist))" in source, (
        "the ship-by date is not rendered with dateIST"
    )
    assert "timeIST(o.ship_by_ist)" not in source, (
        "the ship-by DATE is being formatted as a time, which shifts it into the next day"
    )
    # The other direction: a real instant must keep the time formatter.
    assert "timeIST(o.purchase_date_ist)" in source, (
        "the purchase timestamp lost its time formatting"
    )


def test_date_ist_does_not_construct_a_date_object():
    """Splitting the string is the fix; parsing it is the bug.

    `new Date("2026-08-25")` is UTC midnight by specification, so ANY timezone conversion
    applied afterwards moves the day. dateIST must therefore not build a Date at all.
    """
    source = _orders_source()
    start = source.index("function dateIST(")
    body = source[start:source.index("\n}", start)]
    assert "new Date(" not in body, (
        "dateIST constructs a Date, which reintroduces the UTC-midnight shift"
    )
    assert ".split(" in body, "dateIST should split the ISO date rather than parse it"
