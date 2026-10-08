"""Fetch FBA shipments into customer_order_lines. Nightly: the new day. Backfill: 30-day chunks.

Each chunk is stored and recorded the moment it lands, so a 9-hour backfill interrupted at chunk 12
keeps chunks 1-11, and `repository.done_runs` tells the screen exactly what history is covered.
A failed chunk STOPS the run rather than being skipped: a later chunk recorded as done would make
the coverage look contiguous across a gap that holds no orders.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from app import ist
from app.config import get_settings
from app.database import async_session
from app.repeat import fetch, keys, repository
from app.repeat.parse import parse_rows
from app.shipment.catalogue import load_catalogue

log = logging.getLogger(__name__)
NIGHTLY_DAYS = 7
#: What the screen needs: the 90-day column's 30-day cohort period starts 119 days before `as_of`,
#: and "from other" looks 90 days further back. Plus the 3-day ship lag. Nothing older is fetched.
HISTORY_DAYS = 90 + 30 + 90 + 3
#: A year kept for headroom; rows older than this are deleted.
RETENTION_DAYS = 400
STATE = {"running": False, "phase": "", "error": None, "window": None}


async def run(start: date, end: date, *, db_factory=async_session,
              fetch_rows=fetch.fetch_rows) -> dict:
    if STATE["running"]:
        return {"lines_stored": 0, "error": "already running"}
    STATE.update(running=True, error=None, phase="starting",
                 window=[start.isoformat(), end.isoformat()])
    stored, error = 0, None
    try:
        catalogue, _, _ = await load_catalogue()
        async with db_factory() as db:
            salt = await keys.load_or_create_salt(db)
            mapping = await repository.sku_map(db, catalogue)
        async with httpx.AsyncClient(timeout=get_settings().sp_api_timeout) as client:
            for a, b in fetch.split_days(start, end):
                STATE["phase"] = f"{a} to {b}"
                try:
                    rows = await fetch_rows(a, b, client=client)
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    log.warning("repeat fetch %s..%s failed: %s", a, b, error)
                    async with db_factory() as db:
                        await repository.record_run(db, window_start=a.isoformat(),
                                                    window_end=b.isoformat(), status="failed",
                                                    error=error)
                    break
                lines, counts = parse_rows(rows, salt, mapping)
                async with db_factory() as db:
                    stored += await repository.save_lines(db, lines)
                    await repository.record_run(db, window_start=a.isoformat(),
                                                window_end=b.isoformat(), status="done",
                                                lines_stored=len(lines), **counts)
                log.info("repeat: stored %d line(s) for %s..%s %s", len(lines), a, b, counts)
        async with db_factory() as db:
            await repository.resolve_missing(db, mapping)
    finally:
        try:
            async with db_factory() as db:
                await repository.purge(
                    db, (ist.today() - timedelta(days=RETENTION_DAYS)).isoformat())
        except Exception:
            log.exception("repeat purge failed")
        STATE.update(running=False, phase="", error=error)
    return {"lines_stored": stored, "error": error}


def incremental_start(newest_done: date | None, end: date) -> date:
    """First day the nightly run asks for: the day after the newest stored day, or a week back.

    Asked for as *"keep adding one day per day"*. A routine night adds yesterday; the re-read of
    the six days before it is one report either way and picks up orders that shipped late (an
    order bought on day D ships D+1..D+3). After missed nights it starts at the first missing day,
    so a gap heals on the next run instead of staying. Never before HISTORY_DAYS: nothing older is
    shown, so nothing older is fetched.
    """
    start = end - timedelta(days=NIGHTLY_DAYS - 1)
    if newest_done is None:
        start = end - timedelta(days=HISTORY_DAYS - 1)
    elif newest_done + timedelta(days=1) < start:
        start = newest_done + timedelta(days=1)
    return max(start, end - timedelta(days=HISTORY_DAYS - 1))


async def run_incremental(*, db_factory=async_session, **kw) -> dict:
    end = ist.yesterday()
    async with db_factory() as db:
        runs = await repository.done_runs(db)
    newest = max((date.fromisoformat(b) for _, b in runs), default=None)
    return await run(incremental_start(newest, end), end, db_factory=db_factory, **kw)
