"""The Portfolio Refresh button fetches the ad report ONLY for days `ads_daily` is missing.

Reported as *"app is not working. Could not reach the server"*. A 90-day press re-requested the ACOS
report for all 90 days — three ~15-minute reports in the one web process — while 85 of 87 days were
already stored. The process sat at its 400 MB ceiling (4,669 throttle events in 25 minutes) and every
page took 35-59 seconds until restarted; afterwards 1.6-3.2 seconds.
"""
from datetime import date, timedelta

import pytest

from app.portfolio import ads, refresh

pytestmark = pytest.mark.regression


def _days(start, n):
    d = date.fromisoformat(start)
    return {(d + timedelta(days=i)).isoformat() for i in range(n)}


@pytest.fixture
def held(monkeypatch):
    box = {"days": set()}

    async def fake(_factory=None):
        return box["days"]

    monkeypatch.setattr(refresh, "ads_days_held", fake)
    return box


async def test_THE_OUTAGE_90_days_with_two_missing_asks_for_ONE_short_ad_report(held):
    held["days"] = _days("2026-07-08", 90) - {"2026-09-26", "2026-10-03"}
    plan = await refresh.plan_manual("2026-07-08", "2026-10-05")
    assert (plan["start"], plan["end"]) == ("2026-09-26", "2026-10-03")
    assert (plan["econ_start"], plan["econ_end"]) == ("2026-07-08", "2026-10-05"), (
        "the economics must still be re-read for the whole window: fees post late"
    )
    assert not plan.get("skip_ads")


async def test_a_window_whose_ad_days_are_all_held_asks_for_NO_ad_report(held):
    held["days"] = _days("2026-07-01", 100)
    plan = await refresh.plan_manual("2026-07-08", "2026-10-05")
    assert plan == {"econ_start": "2026-07-08", "econ_end": "2026-10-05", "skip_ads": True}


async def test_a_press_never_asks_for_more_than_ONE_report_and_takes_the_newest_days(held):
    held["days"] = set()                        # a fresh install: all 90 days missing
    plan = await refresh.plan_manual("2026-07-08", "2026-10-05")
    span = (date.fromisoformat(plan["end"]) - date.fromisoformat(plan["start"])).days + 1
    assert span <= ads.MAX_REPORT_DAYS, f"one press asked for {span} days of ad report"
    assert plan["end"] == "2026-10-05", "the newest days must be the ones fetched"


async def test_the_route_runs_the_PLAN_not_the_whole_window(auth_client, monkeypatch, held):
    held["days"] = _days("2026-01-01", 400)
    seen = {}

    async def fake_run(*a, **kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(refresh, "run", fake_run)
    response = await auth_client.post("/portfolio/refresh?start=2026-07-08&end=2026-10-05")
    assert response.status_code == 200, response.text
    import asyncio
    await asyncio.sleep(0)
    assert seen.get("skip_ads") is True and seen.get("econ_start") == "2026-07-08", seen


async def test_ads_days_held_sees_rows_that_carry_a_REAL_sku(db):
    """`ads_daily` rows are per (ASIN, SKU), never at ASIN_GRAIN, so the economics helper's grain
    filter would hold nothing and every press would refetch everything — the outage, silently back.
    Asserted on a stored row rather than on source text: the docstring explaining this names the
    filter, which is exactly how a substring test would pass with the bug present."""
    from app.models import AdsDaily

    db.add(AdsDaily(day="2026-09-30", child_asin="B0AAA00001", seller_sku="0.5kg cs 1 FBA"))
    await db.commit()

    def factory():
        class Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *a):
                return False
        return Ctx()

    assert await refresh.ads_days_held(factory) == {"2026-09-30"}
