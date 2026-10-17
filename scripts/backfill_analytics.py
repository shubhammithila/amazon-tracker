"""One-off: fill Portfolio -> Analytics with every month Amazon still holds (Nov 2024 onwards).

Run on the server in a `screen`:  venv/bin/python scripts/backfill_analytics.py
Resumable: months already stored are skipped. `--all` re-reads every month. Read-only towards Amazon.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ist  # noqa: E402
from app.analytics import logic, refresh  # noqa: E402


async def main() -> int:
    today = ist.today()
    if "--all" in sys.argv:
        months = logic.month_range(refresh.FIRST_MONTH, refresh.recent_months(today)[-1])
    else:
        months = await refresh.missing_months(today)
    if not months:
        print("Every month is already stored.")
        return 0
    print(f"Fetching {len(months)} month(s): {months[0]} .. {months[-1]}")
    result = await refresh.run(months, source="backfill", today=today)
    print(result)
    return 0 if result.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
