"""Storing Amazon orders, and the refresh job that fetches them.

The job is slow by necessity — one getOrders call every 22 seconds — and that single fact
drives every property here: it commits per phase so a failure keeps what it already had,
it refuses to run twice at once, and it reports progress because a silent multi-minute job
is indistinguishable from a broken one.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.orders import refresh, repository

pytestmark = pytest.mark.regression


async def _no_sleep(seconds):
    """Injected everywhere the refresh waits, so a test never spends 22.5 real seconds."""
    return None


def _row(order_id, **overrides):
    row = {
        "amazon_order_id": order_id,
        "purchase_date_utc": datetime(2026, 8, 24, 6, 0),
        "latest_ship_date_utc": datetime(2026, 8, 24, 18, 29),
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


async def test_a_second_refresh_updates_rather_than_duplicating(db, db_schema):
    """The UNIQUE index makes this possible; the upsert makes it happen.

    A refresh runs daily and on demand, so the same order arrives many times. Inserting
    each arrival would multiply every quantity on the picking sheet by the number of
    refreshes — the same double-count the (plan_id, pack_date) index prevents for packing.
    """
    created, updated = await repository.upsert_orders(db, [_row("403-1"), _row("403-2")])
    assert (created, updated) == (2, 0)

    created, updated = await repository.upsert_orders(
        db, [_row("403-1", status="Shipped", easyship_status="PickedUp")]
    )
    assert (created, updated) == (0, 1)

    orders = await repository.load_orders(db)
    assert len(orders) == 2, "a re-refresh duplicated an order"
    changed = next(o for o in orders if o["amazon_order_id"] == "403-1")
    assert changed["status"] == "Shipped", "the update did not apply"
    assert changed["easyship_status"] == "PickedUp"


async def test_first_seen_survives_an_update_but_last_refreshed_moves(db, db_schema):
    """"New since I last looked" and "Amazon changed something" are different questions.

    Overwriting first_seen_at on every refresh would make every order look new every day.
    """
    await repository.upsert_orders(db, [_row("403-1")])
    first = (await repository.load_orders(db))[0]
    original_seen = first["first_seen_at"]

    await repository.upsert_orders(db, [_row("403-1", status="Shipped")])
    after = (await repository.load_orders(db))[0]
    assert after["first_seen_at"] == original_seen, "first_seen_at was overwritten"
    assert after["last_refreshed_at"] >= original_seen


async def test_replacing_items_does_not_accumulate_them(db, db_schema):
    """Items are replaced wholesale, because Amazon's list is the truth.

    Appending instead would double a quantity on the picking sheet every time the items
    were re-fetched — and quantity is what the warehouse picks against.
    """
    await repository.upsert_orders(db, [_row("403-1")])
    items = [{"asin": "B0CHANA500", "seller_sku": "cs-500", "title": "Chana Sattu",
              "quantity_ordered": 2, "quantity_shipped": 0,
              "item_price": 319.0, "item_tax": 15.0, "promotion_discount": 0.0}]

    assert await repository.replace_items(db, "403-1", items) == 1
    assert await repository.replace_items(db, "403-1", items) == 1

    order = (await repository.load_orders(db))[0]
    assert len(order["items"]) == 1, "items accumulated across two fetches"
    assert order["items"][0]["quantity_ordered"] == 2


async def test_storing_items_stamps_items_fetched_at_so_they_are_not_re_fetched(db, db_schema):
    """The flag that keeps a daily refresh cheap.

    getOrderItems costs a call per order. Without this flag, refreshing 100 known orders
    would spend 100 calls to learn nothing — at 2s apart, more than three minutes of
    nothing.
    """
    await repository.upsert_orders(db, [_row("403-1"), _row("403-2")])
    assert sorted(await repository.ids_missing_items(db)) == ["403-1", "403-2"]

    await repository.replace_items(db, "403-1", [
        {"asin": "B0CHANA500", "quantity_ordered": 1}
    ])
    assert await repository.ids_missing_items(db) == ["403-2"], (
        "an order whose items were fetched is still queued for fetching"
    )


async def test_an_order_with_no_items_is_still_marked_fetched(db, db_schema):
    """Otherwise it is retried for ever.

    A cancelled order can legitimately return zero line items. Leaving items_fetched_at
    NULL would queue it again on every refresh, spending a call each time and never
    succeeding differently.
    """
    await repository.upsert_orders(db, [_row("403-1")])
    await repository.replace_items(db, "403-1", [])
    assert await repository.ids_missing_items(db) == []


async def test_purge_removes_only_orders_older_than_the_window(db, db_schema):
    """90-day retention, matching DATA_RETENTION_DAYS and the rest of the app."""
    old = datetime.utcnow() - timedelta(days=200)
    await repository.upsert_orders(db, [
        _row("403-old", purchase_date_utc=old),
        _row("403-new"),
    ])
    assert await repository.purge_older_than(db, 90) == 1
    remaining = [o["amazon_order_id"] for o in await repository.load_orders(db)]
    assert remaining == ["403-new"]


# ─── The refresh job ─────────────────────────────────────────────────────────

async def test_the_refresh_stores_orders_and_their_items(db_schema, monkeypatch):
    """End to end with the network stubbed: orders land, items land, progress is reported."""
    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        return [_row("403-1"), _row("403-2")], []

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        return {oid: [{"asin": "B0CHANA500", "quantity_ordered": 1}] for oid in order_ids}

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    refresh.reset_state()

    result = await refresh.run(days=90)

    assert result["error"] is None, result
    assert result["created"] == 2
    assert result["items_fetched"] == 2
    assert result["running"] is False
    assert result["finished_at"] is not None


async def test_a_second_refresh_is_refused_while_one_runs(db_schema, monkeypatch):
    """Two concurrent refreshes would both burn the 22-second budget and 429 each other.

    Asserted by holding the first inside the fetch and starting the second — a test that
    merely set the flag by hand would not prove the guard is checked on the real path.
    """
    release = asyncio.Event()

    async def slow_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        await release.wait()
        return [_row("403-1")], []

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        return {}

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", slow_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    refresh.reset_state()

    first = asyncio.create_task(refresh.run(days=90))
    await asyncio.sleep(0)                      # let it enter and set the flag
    second = await refresh.run(days=90)

    assert second["refused"] is True, "a concurrent refresh was allowed to start"
    release.set()
    await first
    assert refresh.status()["running"] is False


async def test_a_failure_mid_refresh_keeps_what_was_already_stored(db, db_schema, monkeypatch):
    """A 429 partway through must not throw away minutes of rate-limited work.

    The orders are committed before the item phase begins, so a failure fetching items
    leaves the orders in place and items_fetched_at NULL — the next run resumes rather
    than restarting.
    """
    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        return [_row("403-1"), _row("403-2")], []

    async def exploding_items(order_ids, *, sleep=None, on_item=None):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", exploding_items)
    refresh.reset_state()

    result = await refresh.run(days=90)

    assert result["error"], "the failure was swallowed"
    assert result["running"] is False, "the flag was left set, blocking every later refresh"
    assert result["created"] == 2, "the orders fetched before the failure were lost"
    stored = await repository.load_orders(db)
    assert len(stored) == 2
    assert sorted(await repository.ids_missing_items(db)) == ["403-1", "403-2"], (
        "the next run cannot resume the item fetch"
    )


async def test_warnings_from_paging_reach_the_status(db_schema, monkeypatch):
    """A truncated window must be visible, not inferred from a short list."""
    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        return [_row("403-1")], ["Stopped after 10 pages and Amazon reports more pages"]

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        return {oid: [] for oid in order_ids}

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    refresh.reset_state()

    result = await refresh.run(days=90)
    assert any("more pages" in w for w in result["warnings"])


async def test_actionable_orders_win_the_item_budget(db, db_schema):
    """Items are what the sheet COUNTS, so who wins the cap decides whether it adds up.

    Measured on the live account: 165 of 378 orders were `Delivered` or `ReturnedToSeller`,
    which appear in no section at all. Ordered by purchase date alone they took the item
    budget from the orders awaiting pickup, and the sheet then read 168 units across 265
    orders — fewer units than orders, for products nobody buys in fractions.

    An order with no items is invisible on the sheet even though its section counts the
    order, so this ordering is the difference between a sheet the warehouse can pick against
    and one that quietly understates the work.
    """
    from app.orders import spapi_orders

    # The delivered orders are NEWER, so a purchase-date ordering puts them first.
    await repository.upsert_orders(db, [
        _row("403-old-pickup", status="Shipped", easyship_status="PendingPickUp",
             purchase_date_utc=datetime(2026, 8, 20, 6, 0)),
        _row("403-new-done-1", status="Shipped", easyship_status="Delivered",
             purchase_date_utc=datetime(2026, 8, 24, 6, 0)),
        _row("403-new-done-2", status="Shipped", easyship_status="Delivered",
             purchase_date_utc=datetime(2026, 8, 24, 7, 0)),
    ])

    queued = await repository.ids_missing_items(
        db, limit=1, priority_statuses=spapi_orders.NEEDS_WORK_STATUS_SET
    )
    assert queued == ["403-old-pickup"], (
        f"a delivered order took the item budget from one awaiting pickup: {queued}"
    )


async def test_the_refresh_actually_asks_for_actionable_items_first(
    db, db_schema, monkeypatch
):
    """The wiring, not just the query.

    Asserted separately because the query test passed with the priority argument DROPPED at
    the call site — the ordering was implemented and simply not used, which is the same shape
    as the render target that existed with no `<div>` to draw into. A capability nothing calls
    is not a fix.
    """
    from app.orders import spapi_orders

    await repository.upsert_orders(db, [
        _row("403-pickup", status="Shipped", easyship_status="PendingPickUp"),
    ])

    seen = {}
    real = repository.ids_missing_items

    async def spy(db_, limit=200, *, priority_statuses=None):
        seen["priority_statuses"] = priority_statuses
        return await real(db_, limit, priority_statuses=priority_statuses)

    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        return [_row("403-pickup", status="Shipped", easyship_status="PendingPickUp")], []

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        return {oid: [] for oid in order_ids}

    async def fake_by_id(order_ids, *, sleep=None, on_item=None):
        return []

    monkeypatch.setattr(refresh.repository, "ids_missing_items", spy)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_orders_by_id", fake_by_id)
    refresh.reset_state()

    await refresh.run(days=90, sleep=_no_sleep)

    assert seen.get("priority_statuses") == spapi_orders.NEEDS_WORK_STATUS_SET, (
        "the refresh queues items without prioritising actionable orders, so delivered "
        f"orders can take the whole cap: got {seen.get('priority_statuses')!r}"
    )


# ─── The reconcile pass: the other half of the 247 bug ───────────────────────
#
# The fetch now asks Amazon only for orders with work outstanding, which is what made the
# 371 waiting orders visible. But it means an order that is picked up DISAPPEARS from the
# answer, and an upsert can only correct rows it was given — so without a reconcile pass a
# delivered order would sit on the "waiting for pickup" list for ever. Same class of failure
# as the 247, in the opposite direction.


async def test_an_order_that_dropped_out_of_the_fetch_is_re_read_not_guessed(
    db, db_schema, monkeypatch
):
    """The row must be corrected from AMAZON's answer, never from an assumption.

    403-GONE is held locally as `PendingPickUp` and is absent from the fetch, so something
    changed. What it changed TO is Amazon's to say: this table is a cache of their data, and
    inventing "probably picked up" would make it a second source of truth about whether an
    order shipped — the exact bug the shipment feature's write separation exists to avoid.
    """
    await repository.upsert_orders(db, [
        _row("403-HERE", status="Shipped", easyship_status="PendingPickUp"),
        _row("403-GONE", status="Shipped", easyship_status="PendingPickUp"),
    ])

    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        # Amazon returns only the order that is still awaiting pickup.
        return [_row("403-HERE", status="Shipped", easyship_status="PendingPickUp")], []

    asked = []

    async def fake_by_id(order_ids, *, sleep=None, on_item=None):
        asked.extend(order_ids)
        return [_row("403-GONE", status="Shipped", easyship_status="Delivered")]

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        return {}

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_orders_by_id", fake_by_id)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    refresh.reset_state()

    result = await refresh.run(days=90, sleep=_no_sleep)

    assert asked == ["403-GONE"], f"re-read the wrong orders: {asked}"
    assert result["reconciled"] == 1

    stored = {o["amazon_order_id"]: o for o in await repository.load_orders(db)}
    assert stored["403-GONE"]["easyship_status"] == "Delivered", (
        "a picked-up order still reads as awaiting pickup, so the packer is sent to find a "
        "parcel the courier already took"
    )
    assert stored["403-HERE"]["easyship_status"] == "PendingPickUp", (
        "the order Amazon still reports as awaiting pickup was disturbed"
    )


async def test_a_fetch_that_returned_nothing_reconciles_nothing(db, db_schema, monkeypatch):
    """An empty fetch is a FAILURE, not an empty warehouse.

    Treating it as "every order changed" would re-read the entire backlog — 371 orders, 8
    calls, minutes of the rate budget — to correct rows that were already right, and it would
    do so on exactly the runs where Amazon is already unhappy with us.
    """
    await repository.upsert_orders(db, [
        _row("403-1", status="Shipped", easyship_status="PendingPickUp"),
    ])

    async def empty_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        return [], []

    called = []

    async def fake_by_id(order_ids, *, sleep=None, on_item=None):
        called.append(order_ids)
        return []

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        return {}

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", empty_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_orders_by_id", fake_by_id)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    refresh.reset_state()

    result = await refresh.run(days=90, sleep=_no_sleep)

    assert called == [], "an empty fetch triggered a full re-read of the backlog"
    assert result["reconciled"] == 0


async def test_reconcile_is_bounded_so_it_cannot_re_read_the_whole_history(
    db, db_schema, monkeypatch
):
    """The reconcile pass must stay cheap even though the fetch now asks for delivered orders.

    **This test used to assert that a `Delivered` order was never re-read**, on the reasoning
    that it is absent from the actionable query for ever. That premise is now false and
    deliberately so: excluding the collected statuses is what hid 99 of one day's 194 orders, so
    the fetch asks for them.

    What still has to hold is the COST guarantee. A row is only re-read when we hold it in a
    status the fetch asked for AND a complete fetch did not return it — and the number re-read is
    capped. So a delivered order that Amazon did return (the normal case now) is never re-read,
    and a runaway pass cannot spend the whole rate budget confirming that old orders are still
    old.
    """
    await repository.upsert_orders(db, [
        _row("403-done", status="Shipped", easyship_status="Delivered"),
        _row("403-open", status="Unshipped", easyship_status="PendingSchedule"),
    ])

    # Amazon returns BOTH, which is what the widened filter buys.
    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        return [
            _row("403-open", status="Unshipped", easyship_status="PendingSchedule"),
            _row("403-done", status="Shipped", easyship_status="Delivered"),
        ], []

    called = []

    async def fake_by_id(order_ids, *, sleep=None, on_item=None):
        called.append(list(order_ids))
        return []

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        return {}

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_orders_by_id", fake_by_id)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    refresh.reset_state()

    await refresh.run(days=90, sleep=_no_sleep)

    assert called == [], (
        f"orders the fetch already returned were re-read for nothing: {called}"
    )
    # And the cap exists, so even a pathological run is bounded.
    assert refresh.spapi_orders.RECONCILE_LIMIT <= 200, (
        "the reconcile cap is high enough to spend the rate budget on old orders"
    )


async def test_reconcile_prefers_the_least_recently_refreshed_rows(db, db_schema):
    """The cap means some rows wait for the next run, so the order matters.

    Oldest-refreshed first, because the row least recently confirmed is the one most likely
    to be wrong — and it is the one that has been wrong on screen for longest.
    """
    await repository.upsert_orders(db, [
        _row(f"403-{n}", status="Shipped", easyship_status="PendingPickUp")
        for n in range(5)
    ])
    stale = await repository.ids_needing_reconcile(
        db, {"403-other"}, {"PendingPickUp"}, limit=3
    )
    assert len(stale) == 3, "the cap was not applied"
    assert "403-other" not in stale


# ─── Retention ───────────────────────────────────────────────────────────────

async def test_the_scheduled_sweep_purges_old_orders(db, db_schema, monkeypatch):
    """Orders are purged by the same job that already prunes price_history.

    They need their own call rather than joining that loop: the sweep filters on
    `scraped_at`, and an order has no such column — its age is the PURCHASE date. Wiring it
    into the loop would have raised on the missing attribute, or worse, matched nothing and
    let the table grow without bound the way price_history once did.
    """
    from app.scheduler import scheduled_purge_old_history

    old = datetime.utcnow() - timedelta(days=200)
    await repository.upsert_orders(db, [
        _row("403-ancient", purchase_date_utc=old),
        _row("403-recent"),
    ])
    await repository.replace_items(db, "403-ancient", [
        {"asin": "B0CHANA500", "quantity_ordered": 1}
    ])

    await scheduled_purge_old_history()

    remaining = [o["amazon_order_id"] for o in await repository.load_orders(db)]
    assert remaining == ["403-recent"], f"retention left {remaining}"

    # The items went with it, by cascade — an orphaned item row would keep counting toward
    # a picking sheet for an order that no longer exists.
    from sqlalchemy import func, select

    from app.models import AmazonOrderItem

    orphans = (
        await db.execute(select(func.count()).select_from(AmazonOrderItem))
    ).scalar()
    assert orphans == 0, f"{orphans} item row(s) survived their order"


# ─── Progress reporting: the % bar ───────────────────────────────────────────
#
# A refresh is minutes long — 22.5 seconds a page, then 2.2 seconds an order. A silent job
# is indistinguishable from a broken one, which is why the owner asked for a bar.


async def test_the_percent_climbs_through_the_phases_and_ends_at_100(db_schema, monkeypatch):
    """Each phase moves the bar, and "done" is exactly 100.

    100 is set explicitly rather than computed: the item phase can stop early on a run of
    quota errors, and a bar frozen at 87% beside the word "done" reads as a hang.
    """
    seen = []

    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        if on_page:
            on_page(1, 4, 50)
            seen.append(("page", refresh.STATE["percent"]))
            on_page(2, 4, 90)
            seen.append(("page", refresh.STATE["percent"]))
        return [_row("403-1")], []

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        total = len(order_ids)
        for index, _oid in enumerate(order_ids, start=1):
            if on_item:
                on_item(index, total)
                seen.append(("item", refresh.STATE["percent"]))
        return {oid: [] for oid in order_ids}

    async def fake_by_id(order_ids, *, sleep=None):
        return []

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_orders_by_id", fake_by_id)
    refresh.reset_state()

    result = await refresh.run(days=90, sleep=_no_sleep)

    page_percents = [value for kind, value in seen if kind == "page"]
    item_percents = [value for kind, value in seen if kind == "item"]
    assert page_percents == sorted(page_percents), f"paging went backwards: {page_percents}"
    assert page_percents and page_percents[-1] > 0, "paging reported no progress at all"
    assert item_percents and max(item_percents) > max(page_percents), (
        "the item phase did not advance the bar past the paging phase"
    )
    assert result["percent"] == 100, f"a finished refresh reported {result['percent']}%"


async def test_the_percent_never_steps_backwards(db_schema, monkeypatch):
    """A bar that jumps back reads as a fault.

    Real cause, not hypothetical: the actionable pass can exhaust its token at page 2 of 8,
    and then the PENDING pass starts its own page 1 of 2 — a naive `pages/cap` would drop
    12% back to 25% of a smaller phase, or worse, to 0.
    """
    percents = []

    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        if on_page:
            # 7 of 8 pages = 43% of a 0-50 phase. Then the pending pass reports its OWN
            # page 1 of 2, which computes to 25% — genuinely lower, which is the whole
            # point. An earlier version of this test used 4-of-8 and 1-of-2: both land on
            # exactly 25.0, so it passed with the guard deleted and proved nothing.
            on_page(7, 8, 100)
            percents.append(refresh.STATE["percent"])
            on_page(1, 2, 6)
            percents.append(refresh.STATE["percent"])
        return [_row("403-1")], []

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        return {}

    async def fake_by_id(order_ids, *, sleep=None):
        return []

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_orders_by_id", fake_by_id)
    refresh.reset_state()

    await refresh.run(days=90, sleep=_no_sleep)

    assert percents[1] >= percents[0], (
        f"the bar went backwards when a pass restarted: {percents}"
    )


async def test_the_item_phase_publishes_a_real_denominator(db_schema, monkeypatch):
    """`items_done of items_total` is exact, and the screen says so.

    Worth asserting separately from the percentage: this is the only phase where the total is
    known before the work starts, so it is the only honest count the banner can show.
    """
    async def fake_orders(days=90, *, max_pages=10, sleep=None, on_page=None):
        return [_row("403-1"), _row("403-2"), _row("403-3")], []

    async def fake_items(order_ids, *, sleep=None, on_item=None):
        total = len(order_ids)
        for index, _oid in enumerate(order_ids, start=1):
            if on_item:
                on_item(index, total)
        return {oid: [] for oid in order_ids}

    async def fake_by_id(order_ids, *, sleep=None):
        return []

    monkeypatch.setattr(refresh.spapi_orders, "fetch_easy_ship_orders", fake_orders)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_items", fake_items)
    monkeypatch.setattr(refresh.spapi_orders, "fetch_orders_by_id", fake_by_id)
    refresh.reset_state()

    result = await refresh.run(days=90, sleep=_no_sleep)

    assert result["items_total"] == 3
    assert result["items_done"] == 3


async def test_the_progress_snapshot_is_json_safe(db_schema, monkeypatch):
    """The screen polls this every 3 seconds; a 500 here is a dead bar.

    The same defect already bit once — `status()` returned raw datetimes and the 409 path
    raised "Object of type datetime is not JSON serializable" on production.
    """
    import json

    refresh.reset_state()
    refresh.STATE.update({
        "running": True, "started_at": datetime.utcnow(), "percent": 42,
        "items_total": 10, "items_done": 4, "phase": "items",
    })
    try:
        json.dumps(refresh.status())
    finally:
        refresh.reset_state()
