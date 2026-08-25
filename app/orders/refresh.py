"""The order refresh job: slow, serialized, and resumable.

One `getOrders` call every 22 seconds means a full 90-day refresh is minutes of wall-clock
time. Three properties follow from that, and each exists because the obvious version loses
work:

* **Orders are committed BEFORE the item phase starts.** A 429 while fetching items then
  costs only the items, and the next run resumes from `items_fetched_at IS NULL` rather
  than re-paging everything.
* **Only one refresh runs at a time.** Two would each burn the same 22-second budget and
  429 each other, turning two slow refreshes into two failed ones.
* **Progress is published.** A silent multi-minute job is indistinguishable from a broken
  one, so the screen has something to poll.

State is module-level rather than in the database: it describes THIS process's activity, is
worthless after a restart, and writing it to a row would be a second thing to keep
consistent for no gain.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.database import async_session
from app.orders import repository, spapi_orders

logger = logging.getLogger(__name__)

#: Live progress. Read by GET /orders/refresh-status; never persisted.
STATE: dict = {}


def reset_state() -> None:
    """Back to "never run". Called at import and by tests."""
    STATE.clear()
    STATE.update({
        "running": False,
        "started_at": None,
        "finished_at": None,
        "phase": "idle",
        "pages": 0,
        "orders_seen": 0,
        "created": 0,
        "updated": 0,
        "items_fetched": 0,
        "reconciled": 0,
        "percent": 0,
        "items_total": 0,
        "items_done": 0,
        "warnings": [],
        "error": None,
        "refused": False,
    })


reset_state()

#: Where each phase starts and ends on the 0-100 bar.
#:
#: **The weighting is not even, because the phases are not.** Paging costs 22.5 seconds a
#: call and the item phase 2.2 seconds an order, but there are hundreds of orders and only a
#: handful of pages — on the measured account a full run is roughly 2 minutes of paging and 8
#: of items. The item phase therefore gets the larger share of the bar.
#:
#: The honest asymmetry: **only the item phase has a real denominator.** `len(pending)` is
#: known before the first call, while Amazon reveals `NextToken` one page at a time, so
#: paging progress is a fraction of the page CAP — a ceiling, usually not reached. That makes
#: the first half of the bar an estimate and the second half exact, so the first half is
#: clamped to never step backwards when a pass ends early.
PHASE_BOUNDS = {
    "orders": (0, 50),
    "reconcile": (50, 60),
    "items": (60, 100),
}


def _set_percent(value: float) -> None:
    """Publish a whole-number percent that never goes DOWN.

    Monotonic because a bar that jumps backwards reads as a fault: the actionable pass may
    stop at page 2 of 8 having exhausted its token, which would otherwise send 12% back to 0
    when the pending pass starts its own page 1 of 2.
    """
    percent = max(0, min(100, int(value)))
    if percent > int(STATE.get("percent") or 0):
        STATE["percent"] = percent


def status() -> dict:
    """A copy of the current progress, JSON-safe.

    **The timestamps are converted to ISO strings HERE, not by each caller.** `JSONResponse`
    cannot serialise a `datetime`, so returning the raw dict raised
    `TypeError: Object of type datetime is not JSON serializable` — and it did so on the
    409 "already running" path, which is the one path that fires exactly when the owner is
    trying to find out what is happening. Every route that returns this dict inherits the
    conversion rather than remembering it.

    Found in a browser on production. The test that covered the 409 set `running` by hand
    while `started_at` was still None, so the offending value was never present.
    """
    snapshot = dict(STATE)
    snapshot["warnings"] = list(STATE.get("warnings") or [])
    for field in ("started_at", "finished_at"):
        value = snapshot.get(field)
        if isinstance(value, datetime):
            snapshot[field] = value.isoformat()
    return snapshot


async def run(
    db_factory=async_session,
    *,
    days: int = 90,
    max_pages: int = 10,
    sleep=asyncio.sleep,
) -> dict:
    """Fetch orders and their missing items into the local tables.

    Returns a status snapshot. `refused: True` means a refresh was already running and this
    call did nothing — which is not an error, it is the guard working.

    `days` and `max_pages` differ by caller, deliberately. The half-hourly job looks back a
    couple of weeks over a few pages, because anything still open was placed recently and
    re-paging 90 days every 30 minutes would spend minutes re-learning shipped orders that
    cannot change. The manual button does the deep backfill.

    **The orders commit happens before the item phase**, so a failure fetching items leaves
    every order already stored and merely un-itemised. At 22 seconds a page, re-paging after
    a late failure would waste minutes.

    **`running` is cleared in a `finally`.** A crash that left it set would block every
    later refresh, and the only cure would be restarting the app — a worse failure than the
    one that caused it.
    """
    if STATE.get("running"):
        logger.info("orders refresh: already running, refused")
        snapshot = status()
        snapshot["refused"] = True
        return snapshot

    reset_state()
    STATE.update({"running": True, "started_at": datetime.utcnow(), "phase": "orders"})

    try:
        def on_page(pages_done, page_cap, orders_so_far):
            STATE["pages"] = pages_done
            STATE["orders_seen"] = orders_so_far
            low, high = PHASE_BOUNDS["orders"]
            _set_percent(low + (high - low) * pages_done / max(1, page_cap))

        orders, warnings = await spapi_orders.fetch_easy_ship_orders(
            days=days, max_pages=max_pages, sleep=sleep, on_page=on_page
        )
        STATE["orders_seen"] = len(orders)
        STATE["warnings"] = list(warnings)

        # Committed here, deliberately, before any item call. See the docstring.
        async with db_factory() as db:
            created, updated = await repository.upsert_orders(db, orders)
        STATE.update({"created": created, "updated": updated, "phase": "reconcile"})
        _set_percent(PHASE_BOUNDS["reconcile"][0])

        # ── Correct the rows that dropped OUT of the fetch ──
        #
        # The fetch asks only for orders with work outstanding, so an order that has been
        # picked up is simply absent from the answer — and an upsert can only ever correct
        # rows it was given. Without this pass a delivered order would sit on the
        # "waiting for pickup" list for ever, which is the same class of failure as the 247
        # invisible orders, just in the opposite direction.
        #
        # Amazon is asked what those orders ARE rather than being assumed picked up: this
        # table is a cache of Amazon's data, and guessing a status would make it a second
        # source of truth.
        fetched_ids = {
            row["amazon_order_id"] for row in orders if row.get("amazon_order_id")
        }
        async with db_factory() as db:
            stale = await repository.ids_needing_reconcile(
                db,
                fetched_ids,
                spapi_orders.ACTIONABLE_STATUS_SET,
                limit=spapi_orders.RECONCILE_LIMIT,
            )
        if stale:
            await sleep(spapi_orders.ORDERS_MIN_INTERVAL)
            corrected = await spapi_orders.fetch_orders_by_id(stale, sleep=sleep)
            if corrected:
                async with db_factory() as db:
                    await repository.upsert_orders(db, corrected)
                STATE["reconciled"] = len(corrected)

        STATE["phase"] = "items"
        _set_percent(PHASE_BOUNDS["items"][0])

        # Actionable orders get the item budget first. Items are what the picking sheet
        # counts, and an order with none is invisible on it even though its section counts
        # the order — measured, that reported 168 units across 265 orders because delivered
        # orders had taken the cap.
        async with db_factory() as db:
            pending = await repository.ids_missing_items(
                db, priority_statuses=spapi_orders.ACTIONABLE_STATUS_SET
            )
        if pending:
            STATE["items_total"] = len(pending)

            def on_item(done, total):
                STATE["items_done"] = done
                low, high = PHASE_BOUNDS["items"]
                _set_percent(low + (high - low) * done / max(1, total))

            fetched = await spapi_orders.fetch_items(
                pending, sleep=sleep, on_item=on_item
            )
            async with db_factory() as db:
                for order_id, items in fetched.items():
                    await repository.replace_items(db, order_id, items)
            STATE["items_fetched"] = len(fetched)

        STATE["phase"] = "done"
        # 100 explicitly rather than by arithmetic: the item phase can stop early on a run of
        # quota errors, and a bar stuck at 87% next to the word "done" reads as a hang.
        STATE["percent"] = 100
        logger.info(
            "orders refresh: %d order(s) seen, %d new, %d updated, %d reconciled, "
            "%d itemised",
            len(orders), created, updated, STATE["reconciled"], STATE["items_fetched"],
        )
    except Exception as exc:                    # noqa: BLE001 - reported, not hidden
        # Reported rather than raised: the caller is a background job or a button, and a
        # traceback in a log nobody reads is how a broken refresh survives for days. The
        # orders committed above are kept.
        STATE["error"] = f"{type(exc).__name__}: {exc}"
        STATE["phase"] = "failed"
        logger.exception("orders refresh failed")
    finally:
        STATE["running"] = False
        STATE["finished_at"] = datetime.utcnow()

    return status()
