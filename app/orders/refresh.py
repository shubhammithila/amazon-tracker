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
        "warnings": [],
        "error": None,
        "refused": False,
    })


reset_state()


def status() -> dict:
    """A copy of the current progress, safe for the caller to serialise."""
    snapshot = dict(STATE)
    snapshot["warnings"] = list(STATE.get("warnings") or [])
    return snapshot


async def run(db_factory=async_session, *, days: int = 90, sleep=asyncio.sleep) -> dict:
    """Fetch orders and their missing items into the local tables.

    Returns a status snapshot. `refused: True` means a refresh was already running and this
    call did nothing — which is not an error, it is the guard working.

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
        orders, warnings = await spapi_orders.fetch_easy_ship_orders(days=days, sleep=sleep)
        STATE["orders_seen"] = len(orders)
        STATE["warnings"] = list(warnings)

        # Committed here, deliberately, before any item call. See the docstring.
        async with db_factory() as db:
            created, updated = await repository.upsert_orders(db, orders)
        STATE.update({"created": created, "updated": updated, "phase": "items"})

        async with db_factory() as db:
            pending = await repository.ids_missing_items(db)
        if pending:
            fetched = await spapi_orders.fetch_items(pending, sleep=sleep)
            async with db_factory() as db:
                for order_id, items in fetched.items():
                    await repository.replace_items(db, order_id, items)
            STATE["items_fetched"] = len(fetched)

        STATE["phase"] = "done"
        logger.info(
            "orders refresh: %d order(s) seen, %d new, %d updated, %d itemised",
            len(orders), created, updated, STATE["items_fetched"],
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
