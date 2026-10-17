"""The two Data Kiosk reads behind Analytics. Read-only.

Measured 17 Oct 2026 on the live account: both schemas answer from **Nov 2024** (the first month with
sales, ₹14,341); Oct 2024 is refused, since Amazon keeps about two years. Economics accepts
`aggregateBy.date: MONTH` (a 3-month span came back as 3 × 269 rows). Sales & Traffic's
`salesAndTrafficByAsin` has no date grain, so one month is one query.
"""
from __future__ import annotations

import asyncio
import calendar
from datetime import date, timedelta

from app.config import get_settings
from app.portfolio import economics

TRAFFIC_QUERY = """
query AnalyticsTraffic {
  analytics_salesAndTraffic_2024_04_24 {
    salesAndTrafficByAsin(startDate: "%(start)s", endDate: "%(end)s", aggregateBy: CHILD,
                          marketplaceIds: ["%(market)s"]) {
      startDate endDate parentAsin childAsin
      sales { orderedProductSales { amount } unitsOrdered }
      traffic { sessions pageViews buyBoxPercentage }
    }
  }
}"""


def month_bounds(month: str, today: date) -> tuple[str, str] | None:
    """(first day, last day) of `YYYY-MM`, ending yesterday for the current month; None when the
    month has not started yet (or started today, which holds no complete day)."""
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = min(date(year, mon, calendar.monthrange(year, mon)[1]), today - timedelta(days=1))
    if last < first:
        return None
    return first.isoformat(), last.isoformat()


#: Waits after a quota refusal, in seconds. Data Kiosk lets a burst of queries through and then about
#: one a minute: the first full backfill (17 Oct 2026) was refused after 29 queries in a row.
QUOTA_WAITS = (60, 120, 300)


def _is_quota(exc: Exception) -> bool:
    return getattr(exc, "status", None) == 429 or "quota" in str(exc).lower()


async def _default_run(query: str, label: str, sleep=asyncio.sleep) -> list[dict]:
    """One Data Kiosk query, waiting and retrying when Amazon refuses on quota."""
    for wait in QUOTA_WAITS:
        try:
            return await economics._run_query(query, label=label, sleep=sleep)
        except Exception as exc:  # noqa: BLE001 - only a quota refusal is retried
            if not _is_quota(exc):
                raise
            await sleep(wait)
    return await economics._run_query(query, label=label, sleep=sleep)


async def economics_months(start: str, end: str, run=None) -> list[dict]:
    """Amazon's raw economics rows, one per child ASIN per month, for `start..end`."""
    run = run or _default_run
    query = economics.build_query(start, end, get_settings().sp_api_marketplace_id, by_day=False)
    query = query.replace("date: RANGE", "date: MONTH")
    return await run(query, f"analytics economics {start}..{end}")


async def traffic_month(month: str, today: date, run=None) -> list[dict]:
    """Amazon's raw Sales & Traffic rows for one month (one row per child ASIN with traffic)."""
    run = run or _default_run
    bounds = month_bounds(month, today)
    if bounds is None:
        return []
    query = TRAFFIC_QUERY % {"start": bounds[0], "end": bounds[1],
                             "market": get_settings().sp_api_marketplace_id}
    return await run(query, f"analytics traffic {month}")
