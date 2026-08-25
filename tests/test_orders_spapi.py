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

    assert calls[1].get("NextToken") == "tok-2", "did not follow NextToken"
    assert slept and min(slept) >= spapi_orders.ORDERS_MIN_INTERVAL, (
        f"paged without waiting the rate limit: slept {slept}"
    )
    assert orders, "no orders returned"
    assert all(o["ship_service_level"] and "EZ" in o["ship_service_level"].upper().split()
               for o in orders), "a non-Easy-Ship order survived the filter"
    assert not any(o["amazon_order_id"].startswith("S02-") for o in orders)


# ─── The fourth cause: paging is OLDEST-FIRST ────────────────────────────────


async def test_the_fetch_asks_amazon_for_only_the_actionable_easyship_statuses(monkeypatch):
    """The fix for the bug that survived the first three fixes.

    `getOrders` pages oldest-first and has no sort parameter. Asking by ORDER status over a
    date window returned 165 orders across 6 pages that were every one `Delivered`, while
    371 orders sat waiting in Seller Central and every section of the sheet read zero.

    Filtering on `EasyShipShipmentStatuses` bounds the answer by RELEVANCE instead of by
    date: measured, the same query then returns 97 `PendingSchedule` + 3 `PendingPickUp` on
    page 1, and the complete set is 371 orders in 4 pages.
    """
    seen = []

    async def fake_get(path, params=None, client=None):
        seen.append(params or {})
        return {"payload": {"Orders": []}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    await spapi_orders.fetch_easy_ship_orders(days=90, max_pages=1, sleep=fake_sleep)

    actionable = seen[0]
    assert "EasyShipShipmentStatuses" in actionable, (
        "the fetch does not filter on the Easy Ship status, so oldest-first paging will "
        "fill every page with delivered orders and the picking sheet will read zero"
    )
    requested = set(actionable["EasyShipShipmentStatuses"].split(","))
    assert "PendingPickUp" in requested, "the 264 orders awaiting pickup are not requested"
    assert "PendingSchedule" in requested, "the orders still to pack are not requested"
    # Delivered is the noise that crowded out the real orders; asking for it undoes the fix.
    assert "Delivered" not in requested, (
        "asking for Delivered reintroduces the oldest-first flood that hid 371 orders"
    )
    # MFN drops the FBA orders at Amazon's end. Measured: the unfiltered Pending page held
    # 100 FBA `Expedited` orders and not one Easy Ship order.
    assert actionable.get("FulfillmentChannels") == "MFN"


async def test_pending_payment_orders_get_their_own_pass(monkeypatch):
    """A pending order has NO Easy Ship status, so the actionable filter cannot match it.

    Measured: 6 Easy Ship `Pending` orders on this account, every one with
    `EasyShipShipmentStatus: None`. Filtering on `PendingSchedule,PendingPickUp` therefore
    excludes all of them by construction — so without a second pass the pending-payment
    section is permanently empty, which is the same bug as the 247 in a different disguise.
    """
    seen = []

    async def fake_get(path, params=None, client=None):
        seen.append(params or {})
        return {"payload": {"Orders": []}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    await spapi_orders.fetch_easy_ship_orders(days=90, max_pages=1, sleep=fake_sleep)

    pending = [p for p in seen if p.get("OrderStatuses") == "Pending"]
    assert pending, (
        "no pass asks for Pending orders, so payment-unconfirmed orders can never appear"
    )
    assert "EasyShipShipmentStatuses" not in pending[0], (
        "the pending pass must not filter on an Easy Ship status — a pending order has none, "
        "so the filter would exclude every one of them"
    )


async def test_the_actionable_filter_string_and_the_reconcile_set_cannot_drift(monkeypatch):
    """One list, two consumers. Written twice, they would disagree silently.

    The reconcile pass treats "we hold this status locally but Amazon did not return it" as
    "this order changed". If the set were broader than the query, every extra status would be
    re-read on every refresh for nothing; if narrower, those rows would never be corrected.
    """
    assert spapi_orders.ACTIONABLE_STATUS_SET == frozenset(
        spapi_orders.ACTIONABLE_EASYSHIP_STATUSES.split(",")
    )


async def test_a_truncated_actionable_pass_says_so(monkeypatch):
    """Truncation is REPORTED, and the wording has to be about today's work.

    This is the exact failure the whole rewrite exists to fix, so a silent truncation must
    not be reintroduced by the fix itself.
    """
    page = _fixture("orders_unshipped.json")["payload"]["Orders"]

    async def fake_get(path, params=None, client=None):
        return {"payload": {"Orders": page, "NextToken": "always-more"}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    _orders, warnings = await spapi_orders.fetch_easy_ship_orders(
        days=90, max_pages=2, sleep=fake_sleep
    )
    assert any("missing from today's sheet" in w for w in warnings), warnings


async def test_orders_are_re_read_by_id_in_batches_without_a_date_filter(monkeypatch):
    """How a dropped-out order gets a TRUE status instead of an assumed one.

    The actionable query returns only orders with work outstanding, so a picked-up order
    disappears from it. Asking Amazon by id — verified to work with no date filter — is what
    lets the local row be corrected rather than left saying "waiting for pickup" for ever.
    """
    asked = []

    async def fake_get(path, params=None, client=None):
        asked.append(params or {})
        return {"payload": {"Orders": [
            {"AmazonOrderId": "403-X", "OrderStatus": "Shipped",
             "EasyShipShipmentStatus": "PickedUp",
             "ShipServiceLevel": "Std IN EZ National COD"},
        ]}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    ids = [f"403-{n}" for n in range(spapi_orders.ORDER_ID_BATCH + 5)]
    rows = await spapi_orders.fetch_orders_by_id(ids, sleep=fake_sleep)

    assert len(asked) == 2, "ids were not batched to Amazon's 50-id ceiling"
    assert all("AmazonOrderIds" in p for p in asked)
    assert all("LastUpdatedAfter" not in p for p in asked), (
        "a date filter on an id lookup would hide exactly the orders being reconciled"
    )
    assert len(asked[0]["AmazonOrderIds"].split(",")) == spapi_orders.ORDER_ID_BATCH
    assert rows and rows[0]["easyship_status"] == "PickedUp"


async def test_one_failed_reconcile_batch_keeps_the_others(monkeypatch):
    """These are corrections to rows that already exist, so a partial pass beats none."""
    from app.shipment.spapi import SpApiError

    calls = []

    async def fake_get(path, params=None, client=None):
        calls.append(params)
        if len(calls) == 1:
            raise SpApiError("throttled", status=429)
        return {"payload": {"Orders": [
            {"AmazonOrderId": "403-OK", "OrderStatus": "Shipped",
             "EasyShipShipmentStatus": "Delivered",
             "ShipServiceLevel": "Std IN EZ National COD"},
        ]}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    ids = [f"403-{n}" for n in range(spapi_orders.ORDER_ID_BATCH + 1)]
    rows = await spapi_orders.fetch_orders_by_id(ids, sleep=fake_sleep)

    assert len(calls) == 2, "a failed batch abandoned the rest"
    assert [r["amazon_order_id"] for r in rows] == ["403-OK"]


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
    # Worded in terms of the CONSEQUENCE ("orders are missing from today's sheet") rather
    # than the mechanism ("more pages"), because the person reading it is a warehouse
    # manager deciding whether to trust the sheet in his hand.
    assert any("Amazon reports more" in w for w in warnings), warnings
    assert any("missing from today's sheet" in w for w in warnings), warnings


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


# ─── The fetch bugs that hid 247 orders ──────────────────────────────────────

def test_the_fetch_asks_for_shipped_orders_too():
    """A labelled order is `Shipped`, and that is the whole awaiting-pickup section.

    The first version requested `Unshipped,PartiallyShipped` only. Amazon flips an order to
    `Shipped` the instant a label exists, so all 247 boxes standing in the warehouse were
    never fetched — the section could not populate no matter what the bucketing rule said.
    """
    assert "Shipped" in spapi_orders.OPEN_STATUSES, (
        "Shipped is not fetched, so orders awaiting pickup are invisible"
    )
    assert "Unshipped" in spapi_orders.OPEN_STATUSES
    assert "Pending" in spapi_orders.OPEN_STATUSES, (
        "Pending is not fetched, so the pending-payment section stays empty"
    )
    # Canceled is deliberately excluded: nothing to show, no transition out, and it would
    # spend an item call per parcel that will never exist.
    assert "Canceled" not in spapi_orders.OPEN_STATUSES


async def test_the_fetch_keys_on_last_updated_not_created(monkeypatch):
    """A three-week-old order labelled this morning must reappear today.

    Keyed on `CreatedAfter`, it falls outside a 14-day routine window and silently stops
    being tracked while its parcel sits on the floor. Keyed on `LastUpdatedAfter`, a status
    change brings it back — which is exactly what this screen is watching for.
    """
    seen = {}

    async def fake_get(path, params=None, client=None):
        seen.update(params or {})
        return {"payload": {"Orders": []}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    await spapi_orders.fetch_easy_ship_orders(days=14, max_pages=1, sleep=fake_sleep)

    assert "LastUpdatedAfter" in seen, "the query still keys on order creation"
    assert "CreatedAfter" not in seen


def test_the_recorded_fixture_covers_every_state_the_bucketing_handles():
    """Fixtures are the reason the original bug was invisible to the suite.

    The first capture contained only `PendingSchedule` orders, so no test could have noticed
    that `PendingPickUp` was unhandled — there was nothing in the data to notice. This
    fixture was captured with NO status filter and holds one order per distinct
    (OrderStatus, EasyShipShipmentStatus) pair.
    """
    orders = _fixture("orders_all_states.json")["payload"]["Orders"]
    combos = {(o["OrderStatus"], o.get("EasyShipShipmentStatus")) for o in orders}
    statuses = {o["OrderStatus"] for o in orders}

    assert "Shipped" in statuses, "no Shipped order, so the pickup path is untested"
    assert "Pending" in statuses, "no Pending order, so the pending path is untested"
    assert any(ez == "PendingPickUp" or ez == "PickedUp" for _st, ez in combos), (
        "no post-label Easy Ship status in the fixture"
    )


def test_every_fixture_order_buckets_without_error():
    """Real payloads through the real rule, so a missing field cannot hide.

    Parsing and bucketing are separate steps; this runs them together on genuine Amazon data
    rather than on a hand-built dict that happens to carry every key.
    """
    from datetime import date

    from app.orders import logic

    orders = _fixture("orders_all_states.json")["payload"]["Orders"]
    buckets = set()
    for payload in orders:
        row = spapi_orders.parse_order(payload)
        buckets.add(logic.bucket_for(row, date(2026, 8, 25)))

    assert buckets, "no orders bucketed"
    # The fixture holds Pending, Unshipped, Shipped and Canceled orders, so at minimum the
    # done bucket and one actionable bucket must appear.
    assert logic.BUCKET_DONE in buckets
    assert buckets - {logic.BUCKET_DONE}, (
        "every fixture order was filed as done, which is the bug this file exists to catch"
    )
