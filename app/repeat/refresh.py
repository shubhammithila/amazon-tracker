"""Fetch FBA shipments into customer_order_lines. Nightly: last 7 days. Backfill: 30-day chunks.

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
#: 90-day window + 30-day period + 90-day look-back = 210 days needed; a year kept for headroom.
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


async def run_incremental(**kw) -> dict:
    end = ist.yesterday()
    return await run(end - timedelta(days=NIGHTLY_DAYS - 1), end, **kw)
