"""Parsing and paging the Orders API, against payloads recorded from the live account.

The fixtures in tests/fixtures/orders_*.json were captured on 2026-08-25, not written by
hand. That matters: invented payloads would have missed the two facts that shaped this
feature — that `SellerSKU` is a different namespace from the FBA SKUs in
`pricing_data.json`, and that a non-Easy-Ship order carries a `1995-01-01` ship-by
sentinel which reads as 31 years overdue if treated as a date.

No test here touches the network. `fetch_easy_ship_orders` takes an injectable sleep so
the 22-second rate limit is asserted rather than waited for.
"""
import json
from pathlib import Path

import pytest

from app.orders import spapi_orders

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_the_fixtures_carry_the_cases_that_shaped_the_design():
    """A guard on the other tests: fixtures that lost these are fixtures that prove nothing.

    If someone re-captures them from a quiet day with no `Standard` orders, the EZ filter
    test would pass against input that could not exercise it.
    """
    orders = _fixture("orders_unshipped.json")["payload"]["Orders"]
    levels = {o.get("ShipServiceLevel") for o in orders}
    assert any("EZ" in (level or "") for level in levels), "no Easy Ship order in the fixture"
    assert "Standard" in levels, "no non-Easy-Ship order, so the EZ filter is untested"
    assert any(o.get("LatestShipDate", "").startswith("1995") for o in orders), (
        "the 1995 sentinel is absent, so the sentinel handling is untested"
    )


def test_an_order_payload_maps_onto_the_column_shape():
    """Field names come from Amazon; column names are ours. This is the only translation.

    Asserted against a real payload so a renamed or newly-absent Amazon field fails here,
    in one place, rather than as a NULL column discovered on the picking sheet.
    """
    orders = _fixture("orders_unshipped.json")["payload"]["Orders"]
    ez = next(o for o in orders if "EZ" in (o.get("ShipServiceLevel") or ""))
    row = spapi_orders.parse_order(ez)

    assert row["amazon_order_id"] == ez["AmazonOrderId"]
    assert row["status"] == ez["OrderStatus"]
    assert row["ship_service_level"] == ez["ShipServiceLevel"]
    # Timestamps are parsed to naive UTC datetimes, matching the *_utc columns.
    assert row["latest_ship_date_utc"].tzinfo is None
    assert row["latest_ship_date_utc"].hour == 18, "the UTC hour must survive parsing"
    assert row["order_total"] == pytest.approx(float(ez["OrderTotal"]["Amount"]))
    assert row["city"] == ez["ShippingAddress"]["City"]
    # COD is read off the service level, because PaymentMethod reads "Other" on real
    # COD orders — measured.
    assert row["is_cod"] is ("COD" in ez["ShipServiceLevel"].upper())


def test_a_missing_order_total_does_not_raise():
    """A cancelled order can arrive without OrderTotal or ShippingAddress.

    The refresh must not die on one odd order: that would lose the whole page, and with a
    22-second rate limit a lost page is minutes of work.
    """
    row = spapi_orders.parse_order({"AmazonOrderId": "403-1", "OrderStatus": "Canceled"})
    assert row["amazon_order_id"] == "403-1"
    assert row["order_total"] is None
    assert row["city"] is None
    assert row["latest_ship_date_utc"] is None


def test_item_payloads_keep_the_sku_but_key_on_the_asin():
    """The real SellerSKU is "0.5kg cs 1" — not in pricing_data.json, but its ASIN is.

    Both are stored: the ASIN is how the product is identified, the SKU is what Amazon's
    label shows.
    """
    items = spapi_orders.parse_items(_fixture("orders_items.json"))
    assert items, "no items parsed"
    first = items[0]
    assert first["asin"].startswith("B0")
    assert first["seller_sku"]
    assert first["quantity_ordered"] >= 1


async def test_paging_waits_the_rate_limit_between_calls_and_filters_to_easy_ship(monkeypatch):
    """Two pages means one wait of at least 22 seconds, and only EZ orders survive.

    Amazon returned `x-amzn-RateLimit-Limit: 0.04512` on the live call — one request every
    22.2 seconds. Paging without the wait earns a 429, and on a 90-day window that costs
    the whole refresh.

    The sleep is injected, so this asserts the delay without spending it.
    """
    page = _fixture("orders_unshipped.json")["payload"]["Orders"]
    calls, slept = [], []

    async def fake_get(path, params=None, client=None):
        calls.append(params or {})
        if len(calls) == 1:
            return {"payload": {"Orders": page, "NextToken": "tok-2"}}
        return {"payload": {"Orders": page}}

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)

    orders, warnings = await spapi_orders.fetch_easy_ship_orders(
        days=90, max_pages=5, sleep=fake_sleep
    )

    assert len(calls) == 2, "did not follow NextToken"
    assert calls[1].get("NextToken") == "tok-2"
    assert slept and min(slept) >= spapi_orders.ORDERS_MIN_INTERVAL, (
        f"paged without waiting the rate limit: slept {slept}"
    )
    assert orders, "no orders returned"
    assert all(o["ship_service_level"] and "EZ" in o["ship_service_level"].upper().split()
               for o in orders), "a non-Easy-Ship order survived the filter"
    assert not any(o["amazon_order_id"].startswith("S02-") for o in orders)


async def test_paging_stops_at_max_pages_and_says_so(monkeypatch):
    """An unbounded loop against a rate-limited API is an hours-long request.

    Amazon keeps returning NextToken while there is more; without a cap a first run on a
    large history would page for as long as the tokens last. The cap is REPORTED, so the
    owner knows the window was truncated rather than believing he saw everything.
    """
    page = _fixture("orders_unshipped.json")["payload"]["Orders"]

    async def fake_get(path, params=None, client=None):
        return {"payload": {"Orders": page, "NextToken": "always-more"}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    orders, warnings = await spapi_orders.fetch_easy_ship_orders(
        days=90, max_pages=3, sleep=fake_sleep
    )
    assert any("more pages" in w.lower() for w in warnings), warnings


async def test_fetch_items_asks_only_for_the_ids_it_is_given(monkeypatch):
    """The caller decides which orders need items, and it passes only unfetched ones.

    getOrderItems is cheaper than getOrders but not free, and re-fetching items for 100
    known orders would spend the budget for nothing.
    """
    payload = _fixture("orders_items.json")
    asked = []

    async def fake_get(path, params=None, client=None):
        asked.append(path)
        return payload

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    result = await spapi_orders.fetch_items(["403-A", "403-B"], sleep=fake_sleep)

    assert sorted(result) == ["403-A", "403-B"]
    assert len(asked) == 2
    assert all("/orderItems" in path for path in asked)


async def test_one_failing_order_does_not_lose_the_others(monkeypatch):
    """A 404 on one order must not abandon the batch.

    An order can be cancelled between the list call and the item call. Losing the whole
    batch for that would waste minutes of rate-limited work.
    """
    from app.shipment.spapi import SpApiError

    payload = _fixture("orders_items.json")

    async def fake_get(path, params=None, client=None):
        if "403-BAD" in path:
            raise SpApiError("order not found", status=404)
        return payload

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    result = await spapi_orders.fetch_items(["403-BAD", "403-OK"], sleep=fake_sleep)

    assert "403-OK" in result, "a good order was lost because another failed"
    assert "403-BAD" not in result
