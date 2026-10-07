"""One-off: fill customer_order_lines with ~18 months, 30 days at a time. Resumable.

Run on the server in `screen`:  cd /opt/amazon-tracker && venv/bin/python scripts/backfill_repeat.py
Each 30-day chunk takes 30-40 min (Amazon's report queue is serial), so 18 months is ~9-12 hours.
Chunks go NEWEST FIRST, so the 30- and 60-day columns fill within the first few hours. Chunks
already recorded as done are skipped, so re-running after an interruption simply continues.
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ist  # noqa: E402
from app.database import async_session  # noqa: E402
from app.repeat import fetch, refresh, repository  # noqa: E402
from app.repeat.logic import covered_from  # noqa: E402

MONTHS = 18


async def main() -> int:
    end = ist.yesterday()
    start = end - timedelta(days=MONTHS * 30)
    async with async_session() as db:
        done = set(await repository.done_runs(db))
    for a, b in reversed(fetch.split_days(start, end)):
        if (a.isoformat(), b.isoformat()) in done:
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
