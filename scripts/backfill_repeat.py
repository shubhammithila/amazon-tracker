"""One-off: fill customer_order_lines with the history the screen needs, 30 days at a time.

Only `refresh.HISTORY_DAYS` (~7 months) is fetched: that is what the 90-day column reaches back to,
and asking for more spends Amazon's report quota on days nothing reads. It was 18 months, and the
eleventh 30-day request in three hours was refused with "You exceeded your quota".

Run on the server in `screen`:  cd /opt/amazon-tracker && venv/bin/python scripts/backfill_repeat.py
Each 30-day chunk takes 15-40 min (Amazon's report queue is serial), so ~2-4 hours.
Chunks go NEWEST FIRST, so the 30- and 60-day columns fill within the first few hours. Chunks
already recorded as done are skipped, so re-running after an interruption simply continues.

`--reprice` re-reads EVERY month since 1 Jan 2026 whose lines still lack their price (Customer
value -> LTV needs what each customer paid, which lines stored before d2f6b8a41c07 do not carry).
Resumable the same way: a chunk whose lines are all priced is skipped. ~10 chunks, ~3-6 hours.
"""
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ist  # noqa: E402
from app.database import async_session  # noqa: E402
from app.repeat import fetch, refresh, repository  # noqa: E402
from app.repeat.logic import covered_from  # noqa: E402
from app.repeat.value_service import HISTORY_START  # noqa: E402
from app.models import CustomerOrderLine  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


async def _unpriced(a, b) -> int:
    async with async_session() as db:
        return (await db.execute(
            select(func.count()).select_from(CustomerOrderLine)
            .where(CustomerOrderLine.purchase_day.between(a.isoformat(), b.isoformat()),
                   CustomerOrderLine.revenue.is_(None)))).scalar() or 0


def _covered(a, b, done) -> bool:
    days = [(date.fromisoformat(x), date.fromisoformat(y)) for x, y in done]
    d = a
    while d <= b:
        if not any(x <= d <= y for x, y in days):
            return False
        d += timedelta(days=1)
    return True


async def main() -> int:
    reprice = "--reprice" in sys.argv
    end = ist.yesterday()
    start = (date.fromisoformat(HISTORY_START) if reprice
             else end - timedelta(days=refresh.HISTORY_DAYS - 1))
    async with async_session() as db:
        done = set(await repository.done_runs(db))
    for a, b in reversed(fetch.split_days(start, end)):
        # Skip a chunk whose every day is already inside some stored window, not only an exact
        # match: the chunk boundaries move with the date, so exact matching would refetch it all.
        if _covered(a, b, done) and (not reprice or await _unpriced(a, b) == 0):
            print("skip", a, b)
            continue
        print("fetch", a, b, flush=True)
        result = await refresh.run(a, b)
        print("  ", result, flush=True)
        if result.get("error"):
            print("stopping; re-run to continue from here")
            return 1
    async with async_session() as db:
        runs = await repository.done_runs(db)
    print("contiguous history from", covered_from(runs, end - timedelta(days=3)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
