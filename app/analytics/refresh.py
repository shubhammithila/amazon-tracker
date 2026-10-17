"""Fetch Analytics months from Amazon and store them. Read-only towards Amazon.

Economics is asked for in spans of up to three months (one `MONTH` query each) and traffic one month
per query. Each span is stored the moment it lands, so a long backfill interrupted part-way keeps
what it has. A failed span STOPS the run and names its months: carrying on would leave a gap that
looks like a month with no sales.

`finally` clears the running flag, so a cancelled task cannot leave every later refresh refused.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app import ist
from app.analytics import fetch, logic, repository
from app.database import async_session

log = logging.getLogger(__name__)
#: The first month Data Kiosk answers for this account (measured 17 Oct 2026: Oct 2024 is refused,
#: Nov 2024 is the first month with sales).
FIRST_MONTH = "2024-11"
SPAN_MONTHS = 3
STATE = {"running": False, "phase": "", "error": None}


def _chunks(months: list[str], size: int) -> list[list[str]]:
    return [months[i:i + size] for i in range(0, len(months), size)]


def recent_months(today: date) -> list[str]:
    """The current month and the previous one: fees on last month are still posting."""
    current = today.strftime("%Y-%m")
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return [prev, current] if today.day > 1 else [prev]


async def run(months: list[str], *, source: str = "nightly", today: date | None = None,
              run_query=None) -> dict:
    """Fetch and store `months`. Returns {"status", "months", "error"}; {"refused": True} if busy."""
    if STATE["running"]:
        return {"refused": True}
    today = today or ist.today()
    months = sorted(m for m in months if fetch.month_bounds(m, today))
    STATE.update(running=True, error=None, phase="starting")
    status, error = "done", None
    async with async_session() as db:
        run_row = await repository.start_run(db, source, months)
        run_id = run_row.id
    try:
        for span in _chunks(months, SPAN_MONTHS):
            start = fetch.month_bounds(span[0], today)[0]
            end = fetch.month_bounds(span[-1], today)[1]
            STATE["phase"] = f"sales {span[0]} to {span[-1]}"
            raw = await fetch.economics_months(start, end, run=run_query)
            async with async_session() as db:
                await repository.save_economics(db, repository.rows_from_economics(raw), span)
                await db.commit()
            for month in span:
                STATE["phase"] = f"traffic {month}"
                rows = repository.rows_from_traffic(await fetch.traffic_month(month, today, run=run_query), month)
                async with async_session() as db:
                    await repository.save_traffic(db, rows, month)
                    await db.commit()
    except Exception as exc:  # noqa: BLE001 - recorded and shown on the tab
        status, error = "failed", (str(exc) or type(exc).__name__)[:500]
        STATE["error"] = error
        log.warning("analytics refresh failed at %s: %s", STATE["phase"], error)
    finally:
        STATE["running"] = False
        async with async_session() as db:
            from app.models import AnalyticsRefresh
            row = await db.get(AnalyticsRefresh, run_id)
            row.status, row.error, row.finished_at = status, error, datetime.utcnow()
            await db.commit()
    return {"status": status, "months": months, "error": error}


async def run_recent(source: str = "nightly", today: date | None = None, run_query=None) -> dict:
    today = today or ist.today()
    return await run(recent_months(today), source=source, today=today, run_query=run_query)


async def missing_months(today: date | None = None) -> list[str]:
    """Months from FIRST_MONTH to last month missing their sales OR their traffic, oldest first."""
    today = today or ist.today()
    async with async_session() as db:
        held = await repository.months_held(db)
    last = recent_months(today)[0]
    return [m for m in logic.month_range(FIRST_MONTH, last)
            if m not in held["economics"] or m not in held["traffic"]]
