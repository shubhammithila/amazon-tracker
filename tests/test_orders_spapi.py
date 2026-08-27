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
from datetime import datetime, timezone
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


async def test_the_fetch_filters_on_the_easyship_status_and_the_window(monkeypatch):
    """The fix for the bug that survived the first three fixes.

    `getOrders` pages oldest-first and has no sort parameter, so filtering is what makes the
    answer reachable at all: asking by ORDER status over a date window returned 165 orders across
    6 pages that were every one `Delivered`, while 371 sat waiting in Seller Central.

    **This test used to assert `Delivered` was NOT requested, and that later became wrong.** The
    reasoning then was that delivered orders are the flood — true while the window was 90 days.
    But excluding the collected statuses hid 99 of one day's 194 orders, because Amazon collects
    within hours and a `PickedUp` order was therefore never in the answer. What prevents the
    flood is the narrow WINDOW (`TODAY_ONLY`), not a narrow status list; the two knobs were
    conflated. Now asserted as "filters on both axes", so the status list can be corrected
    without rewriting this test's premise a third time.
    """
    seen = []

    async def fake_get(path, params=None, client=None):
        seen.append(params or {})
        return {"payload": {"Orders": []}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    await spapi_orders.fetch_easy_ship_orders(
        days=spapi_orders.TODAY_ONLY, max_pages=1, sleep=fake_sleep
    )

    actionable = seen[0]
    assert "EasyShipShipmentStatuses" in actionable, (
        "the fetch does not filter on the Easy Ship status, so oldest-first paging will fill "
        "every page with orders that are long gone and the sheet will read zero"
    )
    requested = set(actionable["EasyShipShipmentStatuses"].split(","))
    assert "PendingPickUp" in requested, "orders awaiting the courier are not requested"
    assert "PendingSchedule" in requested, "the orders still to label are not requested"
    # MFN drops the FBA orders at Amazon's end. Measured: the unfiltered Pending page held
    # 100 FBA `Expedited` orders and not one Easy Ship order.
    assert actionable.get("FulfillmentChannels") == "MFN"
    # The WINDOW is what bounds the cost — see test_today_only_resolves_to_midnight_ist.
    assert actionable["LastUpdatedAfter"] == spapi_orders._since(spapi_orders.TODAY_ONLY)


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
    assert spapi_orders.FETCHED_STATUS_SET == frozenset(
        spapi_orders.ACTIONABLE_EASYSHIP_STATUSES.split(",")
    )


async def test_a_truncated_actionable_pass_says_so(monkeypatch):
    """Truncation is REPORTED — never silent.

    **This used to demand the phrase "missing from today's sheet", and that phrase was FALSE.**
    The requirement was right — a silent truncation is the failure the rewrite exists to fix —
    but it was pinned to one sentence, and the sentence misdescribed what truncation costs.

    `getOrders` pages OLDEST-FIRST while the window asks for everything UPDATED today, so the
    rows lost to the cap are the OLDEST. Today's dispatch, being the most recently updated by
    definition, is on the pages that DID arrive. The old message therefore sent the warehouse
    hunting every half hour for parcels that were on screen all along.

    So this asserts the REQUIREMENT (something is reported, and it names the cause) rather than
    the conclusion — the third time this file has had to learn that distinction.
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
    assert warnings, "the pass truncated and said nothing"
    joined = " ".join(warnings)
    assert "2 pages" in joined, f"the warning does not say how far the pass got: {warnings}"
    assert "oldest" in joined.lower(), (
        f"the warning does not say WHICH orders were dropped: {warnings}"
    )
    assert "missing from today" not in joined, (
        "the warning claims today's sheet is incomplete. Oldest-first paging means today's "
        "orders are precisely the ones that WERE fetched, so this is a false alarm the "
        "warehouse acts on every half hour"
    )


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
    # The cap must be OBSERVED and REPORTED. Both halves matter: a cap that is not enforced is
    # an hours-long request against a 22.5-second rate limit, and one that is not reported is a
    # sheet the owner trusts more than he should.
    #
    # Asserted via the reported page count rather than by multiplying the fixture size, because
    # two other behaviours also shape `orders` here and made the obvious arithmetic wrong: the
    # 6-order fixture holds only 4 Easy Ship orders (`is_easy_ship` drops the rest), and
    # `seen_ids` de-duplicates the same page handed back three times. Counting orders would
    # therefore assert those two mechanisms rather than the cap.
    assert any("3 pages" in w for w in warnings), warnings
    # NOT "missing from today's sheet" — see test_a_truncated_actionable_pass_says_so for why
    # that wording was false. Oldest-first paging drops the OLDEST orders, not today's.
    assert "missing from today" not in " ".join(warnings), warnings


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


def test_the_item_interval_leaves_margin_below_the_measured_limit():
    """Amazon returns `x-amzn-RateLimit-Limit: 0.5` for getOrderItems — one per 2.0s exactly.

    This was 2.0, sitting ON the limit with nothing spare, under a comment guessing that
    "3-4 new orders a day" made it academic. The real backlog is 235 orders needing items,
    and a 200-call run at dead-on 2.0s earned "You exceeded your quota" on 54 of them.

    A dropped item call is not cosmetic: the order still appears in its section while
    contributing no units, so the sheet understates what has to be picked.
    """
    assert spapi_orders.ITEMS_MIN_INTERVAL > 2.0, (
        "pacing exactly at Amazon's measured limit leaves no room for jitter, and the "
        "symptom is a picking sheet that silently undercounts units"
    )


async def test_a_run_of_quota_errors_gives_up_instead_of_grinding(monkeypatch):
    """Once the bucket is empty every further call fails identically.

    Measured: 54 consecutive failures in one batch — two minutes of wall clock and 54
    requests against an account Amazon is already throttling. The skipped orders keep
    `items_fetched_at` NULL, so stopping early is what lets the next run succeed sooner.
    """
    from app.shipment.spapi import SpApiError

    calls = []

    async def fake_get(path, params=None, client=None):
        calls.append(path)
        raise SpApiError("Amazon said: You exceeded your quota for the requested resource.",
                         status=429)

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    result = await spapi_orders.fetch_items(
        [f"403-{n}" for n in range(200)], sleep=fake_sleep
    )

    assert result == {}
    assert len(calls) == spapi_orders.ITEMS_THROTTLE_GIVE_UP, (
        f"kept calling a throttled endpoint {len(calls)} times"
    )


async def test_a_single_404_does_not_stop_the_batch(monkeypatch):
    """The give-up rule must not fire on one cancelled order.

    An order can be cancelled between the list call and this one. That is a 404, not an empty
    bucket, and abandoning the batch for it would waste minutes of rate-limited work.
    """
    from app.shipment.spapi import SpApiError

    payload = _fixture("orders_items.json")
    seen = []

    async def fake_get(path, params=None, client=None):
        seen.append(path)
        if "403-BAD" in path:
            raise SpApiError("order not found", status=404)
        return payload

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    ids = ["403-BAD"] * 8 + ["403-OK"]
    result = await spapi_orders.fetch_items(ids, sleep=fake_sleep)

    assert "403-OK" in result, "a run of 404s stopped the batch as though it were throttling"
    assert len(seen) == len(ids)


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


# ─── The 99 orders that were collected before we ever fetched them ────────────
#
# Seller Central showed 194 orders picked up today; the screen showed 95. Not a display bug:
# `PendingSchedule,PendingPickUp` never returned them. Amazon collects within hours, so an order
# labelled AND collected between two refreshes was never in the answer — and the reconcile pass
# could not rescue it, because that only re-reads orders already held.


def test_the_fetch_asks_for_collected_orders_too():
    """`PickedUp` must be in the filter, or a whole day's dispatch can be invisible.

    Measured on 2026-08-26: `PendingPickUp` returned **0** — every one of the day's orders had
    already been collected — while 193 orders carried a ship-by of today. The fetch has to agree
    with `logic.is_todays_dispatch`, which counts a collected order as still today's work,
    because the boxes were packed on this floor this morning.
    """
    requested = set(spapi_orders.ACTIONABLE_EASYSHIP_STATUSES.split(","))
    assert "PickedUp" in requested, (
        "collected orders are not requested, so a day whose orders are all picked up shows as "
        "empty — 95 of 194 was the measured symptom"
    )
    assert "PendingSchedule" in requested, "orders still to label are not requested"
    assert "PendingPickUp" in requested, "orders awaiting the courier are not requested"


def test_the_fetch_filter_covers_every_status_the_dispatch_rule_accepts():
    """The wire filter and the local predicate must not disagree.

    `logic.is_todays_dispatch` accepts `LABELLED_EASYSHIP`; if the fetch asks for less, the
    predicate never sees those rows and the screen undercounts. This is the coupling that broke:
    the predicate already accepted `PickedUp`, and the fetch did not.

    The doc aliases are deliberately NOT sent to Amazon — an unrecognised status risks the whole
    query rather than merely matching nothing — so they are excluded from the comparison.
    """
    from app.orders import logic

    requested = {s.strip() for s in spapi_orders.ACTIONABLE_EASYSHIP_STATUSES.split(",")}
    # Never observed on amazon.in; kept locally as harmless aliases only.
    doc_aliases = {"LabelGenerated", "ReadyForPickup"}
    accepted = logic.LABELLED_EASYSHIP - doc_aliases

    missing = accepted - requested
    assert not missing, (
        f"the dispatch rule accepts {sorted(missing)} but the fetch never asks for them, so "
        "those orders can only reach the screen by accident"
    )


def test_today_only_resolves_to_midnight_ist_not_midnight_utc():
    """The business day starts at 00:00 IST, which is 18:30Z the PREVIOUS day.

    Using the UTC day boundary would drop every order placed between 00:00 and 05:30 IST from
    the routine refresh — the same 5.5-hour class of error the `*_utc` column suffix exists to
    prevent.
    """
    from app.orders import logic

    since = spapi_orders._since(spapi_orders.TODAY_ONLY)
    parsed = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ")
    as_ist = parsed.replace(tzinfo=timezone.utc).astimezone(logic.IST)

    assert (as_ist.hour, as_ist.minute) == (0, 0), (
        f"the window starts at {as_ist:%H:%M} IST, not midnight"
    )
    assert as_ist.date() == datetime.now(logic.IST).date(), (
        "the window does not start at the beginning of TODAY in IST"
    )


def test_a_numeric_window_is_still_a_count_of_days_back():
    """The manual button passes 3, and that must keep meaning three days."""
    since = spapi_orders._since(3)
    parsed = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - parsed
    assert 2.9 < delta.total_seconds() / 86400 < 3.1, f"3 days resolved to {delta}"


async def test_the_fetch_uses_the_window_helper_for_both_kinds_of_window(monkeypatch):
    """`TODAY_ONLY` has to survive the trip through `fetch_easy_ship_orders`.

    Asserted on the outgoing parameter rather than on the helper alone, because the bug this
    guards is the fetch computing its own `since` and ignoring the sentinel — which is exactly
    what it did before, with `timedelta(days=days)` and a string that would have raised.
    """
    seen = []

    async def fake_get(path, params=None, client=None):
        seen.append(params or {})
        return {"payload": {"Orders": []}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    await spapi_orders.fetch_easy_ship_orders(
        days=spapi_orders.TODAY_ONLY, max_pages=1, sleep=fake_sleep
    )

    assert seen, "no request was made"
    assert seen[0]["LastUpdatedAfter"] == spapi_orders._since(spapi_orders.TODAY_ONLY), (
        "the sentinel window did not reach Amazon; the fetch is computing its own date"
    )
