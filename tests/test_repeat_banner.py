"""The Repeat tab warns only when its data is actually OLD.

Reported as *"The last refresh failed: Amazon said: You exceeded your quota… I dont want this
error"*. On 8 Oct the failed run was a backfill chunk for Dec-Jan, refused after every day the
screen needs was already stored, and the banner led the page above complete figures.
"""
from datetime import date, timedelta

import pytest

from app.database import async_session
from app.repeat import repository, service

pytestmark = pytest.mark.regression
TODAY = date(2026, 10, 8)


@pytest.fixture(autouse=True)
def no_catalogue(monkeypatch):
    async def fake():
        return {}, None, "none"
    monkeypatch.setattr("app.repeat.service.load_catalogue", fake)


async def _run(start, end, status="done", error=None):
    async with async_session() as db:
        await repository.record_run(db, window_start=start.isoformat(), window_end=end.isoformat(),
                                    status=status, error=error)


async def _payload():
    async with async_session() as db:
        return await service.build_payload(db, None, TODAY)


async def test_an_OLD_chunk_failing_after_the_recent_days_landed_is_not_reported(db_schema):
    await _run(date(2026, 10, 1), date(2026, 10, 7))
    await _run(date(2025, 12, 11), date(2026, 1, 9), "failed", "Amazon said: You exceeded your quota")
    got = await _payload()
    assert got["stale_days"] == 0
    assert got["last_refresh"]["status"] == "done", "an old backfill chunk spoke for today's data"


async def test_old_data_IS_reported_with_the_reason(db_schema):
    await _run(date(2026, 9, 1), date(2026, 10, 1))
    await _run(date(2026, 10, 2), date(2026, 10, 7), "failed", "Amazon said: You exceeded your quota")
    got = await _payload()
    assert got["stale_days"] == 6 and got["newest_day"] == "2026-10-01"
    assert got["last_refresh"]["status"] == "failed"


async def test_a_day_or_two_behind_is_normal_and_not_a_banner(db_schema):
    await _run(date(2026, 9, 1), TODAY - timedelta(days=1 + service.STALE_AFTER_DAYS))
    assert (await _payload())["stale_days"] == 0


def test_the_page_banners_on_staleness_not_on_any_failed_run():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "templates" / "portfolio_repeat.html").read_text(encoding="utf-8")
    body = src[src.index("async function load("):src.index('$("brand").addEventListener')]
    assert '$("messages").innerHTML = data.stale_days' in body
