"""The background economics refresh, and the progress the screen polls.

**Never awaited inside a request.** A Data Kiosk query takes one to two minutes (measured), so
a route that awaited it would hold the connection open and time out behind Caddy. The route
starts this with ``asyncio.create_task`` and returns at once; the screen polls
``/portfolio/refresh-status``.

Deliberately the same shape as ``app.orders.refresh``: a module-level ``STATE``, a monotonic
percentage, ``status()`` that converts datetimes to ISO strings, and a concurrency guard that
REFUSES rather than raising. Two progress implementations that differ in their details are two
things to learn, and the orders one has already had its bugs found on production.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.database import async_session
from app.portfolio import economics, repository
from app.shipment.spapi import SpApiError, SpApiNotConfigured

logger = logging.getLogger(__name__)

#: Live progress for the banner. Module-level because there is exactly one refresh at a time
#: and the screen polls a separate request that must see it.
STATE: dict = {}


def reset_state() -> None:
    """Back to idle. Called at import and by tests."""
    STATE.clear()
    STATE.update({
        "running": False,
        "started_at": None,
        "finished_at": None,
        "phase": "idle",        # "submit" | "poll" | "download" | "store" | "done" | "failed"
        "percent": 0,
        "rows": 0,
        "window_start": None,
        "window_end": None,
        "error": None,
        "refused": False,
    })


reset_state()

#: How the bar is divided. **Uneven because the phases are.** Submitting is one call; polling is
#: where the whole 1-2 minutes goes; downloading and storing are seconds. Giving poll 70% of the
#: bar means the bar moves while the owner is actually waiting.
#:
#: The honest part: **no phase here has a true denominator.** Amazon publishes no progress for a
#: running query, so poll progress is `attempt / POLL_MAX` — a fraction of a CEILING that is
#: usually not reached, which makes the bar deliberately pessimistic. It reaches 100 only when
#: the rows are stored, so a bar at 80 that finishes early is expected rather than a fault.
PHASE_BOUNDS = {
    "submit": (0, 10),
    "poll": (10, 80),
    "download": (80, 92),
    "store": (92, 100),
}


def _set_percent(value: float) -> None:
    """Publish a whole-number percent that never goes DOWN.

    Monotonic for the same reason the orders bar is: a bar that steps backwards reads as a
    fault. Here it matters because the poll phase is a fraction of a ceiling — a query that
    finishes on attempt 3 of 40 jumps from 15% to the download phase, and nothing may later
    report a lower number.
    """
    percent = max(0, min(100, int(value)))
    if percent > int(STATE.get("percent") or 0):
        STATE["percent"] = percent


def _progress(phase: str, done: int, total: int) -> None:
    """Map one phase's (done, total) onto its slice of the bar."""
    STATE["phase"] = phase
    low, high = PHASE_BOUNDS.get(phase, (0, 100))
    fraction = (done / total) if total else 0
    _set_percent(low + (high - low) * fraction)


def status() -> dict:
    """A copy of the current progress, JSON-safe.

    **Datetimes become ISO strings HERE, not in each caller.** `JSONResponse` cannot serialise a
    datetime, and the orders feature shipped exactly that bug — on the "already running" path,
    which is the one path that fires when someone is trying to find out what is happening.
    """
    snapshot = dict(STATE)
    for field in ("started_at", "finished_at"):
        value = snapshot.get(field)
        if isinstance(value, datetime):
            snapshot[field] = value.isoformat()
    return snapshot


async def run(db_factory=async_session, *, days: int | None = None, sleep=asyncio.sleep) -> dict:
    """Fetch the economics window and store it. Returns a status snapshot.

    **Refuses rather than raises when one is already running**, so the nightly job overlapping a
    manual refresh is a no-op instead of an error in the log every night. The guard lives here
    rather than in the route, so every caller inherits it.

    ``sleep`` is injectable so a test can drive the whole submit/poll/download sequence without
    spending minutes.
    """
    if STATE.get("running"):
        logger.info("portfolio refresh: already running, refused")
        snapshot = status()
        snapshot["refused"] = True
        return snapshot

    reset_state()
    started = datetime.utcnow()
    STATE.update({"running": True, "started_at": started, "phase": "submit"})

    window_start = window_end = None
    try:
        rows, window_start, window_end = await economics.fetch_economics(
            days=days or economics.WINDOW_DAYS, sleep=sleep, on_progress=_progress
        )
        STATE.update({"window_start": window_start, "window_end": window_end})

        _progress("store", 0, 1)
        async with db_factory() as db:
            stored = await repository.save_snapshot(db, window_start, window_end, rows)
            await repository.record_refresh(
                db, window_start=window_start, window_end=window_end,
                rows_stored=stored, started_at=started,
            )
        _progress("store", 1, 1)

        STATE.update({"rows": stored, "phase": "done", "percent": 100})
        logger.info(
            "portfolio refresh: %d row(s) stored for %s..%s", stored, window_start, window_end
        )
    except SpApiNotConfigured:
        # Not an error worth a stack trace: the app is expected to work without Amazon
        # credentials, and the screen says so rather than the log filling up.
        STATE.update({"phase": "failed", "error": "Amazon credentials are not configured."})
        logger.info("portfolio refresh: skipped, SP-API is not configured")
    except SpApiError as exc:
        # Amazon's own message is surfaced verbatim — it is written for a developer and has been
        # the most useful thing at every step of this integration.
        STATE.update({"phase": "failed", "error": str(exc)})
        logger.warning("portfolio refresh failed: %s", exc)
        async with db_factory() as db:
            await repository.record_refresh(
                db, window_start=window_start, window_end=window_end,
                rows_stored=0, error=str(exc), started_at=started,
            )
    except Exception as exc:                    # noqa: BLE001 - a crash must not wedge the flag
        # Without this the `running` flag would stay True for the life of the process and every
        # later refresh — including the nightly one — would be refused. The orders refresh
        # carries the same guard for the same reason.
        STATE.update({"phase": "failed", "error": f"Unexpected failure: {exc}"})
        logger.exception("portfolio refresh crashed")
    finally:
        STATE["running"] = False
        STATE["finished_at"] = datetime.utcnow()

    return status()
