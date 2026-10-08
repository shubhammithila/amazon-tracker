import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models import CustomerOrderLine, RepeatRefresh
from app.repeat import refresh

pytestmark = pytest.mark.regression
ROW = {"amazon-order-id": "171-1", "shipment-item-id": "D1",
       "purchase-date": "2026-09-01T05:00:00+00:00", "buyer-email": "a@marketplace.amazon.in",
       "sku": "1kg cs FBA", "quantity-shipped": "1", "item-price": "90"}


@pytest.fixture(autouse=True)
def no_catalogue(monkeypatch):
    async def fake():
        return {}, None, "none"
    monkeypatch.setattr("app.repeat.refresh.load_catalogue", fake)


async def test_each_chunk_is_stored_and_recorded_as_it_lands(db_schema, count_rows):
    seen = []

    async def fake_fetch(start, end, *, client):
        seen.append((start, end))
        return [dict(ROW, **{"amazon-order-id": f"o{start.isoformat()}"})]
    result = await refresh.run(date(2026, 7, 1), date(2026, 9, 15), fetch_rows=fake_fetch)
    assert len(seen) == 3 and result == {"lines_stored": 3, "error": None}
    assert await count_rows(CustomerOrderLine) == 3
    assert await count_rows(RepeatRefresh, status="done") == 3


async def test_a_failed_chunk_keeps_earlier_chunks_and_STOPS(db_schema, count_rows, read_committed):
    calls = []

    async def flaky(start, end, *, client):
        calls.append(start)
        if len(calls) == 2:
            raise RuntimeError("Amazon reported FATAL")
        return [dict(ROW, **{"amazon-order-id": f"o{len(calls)}"})]
    result = await refresh.run(date(2026, 7, 1), date(2026, 9, 15), fetch_rows=flaky)
    assert "FATAL" in result["error"] and len(calls) == 2      # chunk 3 never requested
    assert await count_rows(CustomerOrderLine) == 1

    async def statuses(session):
        return sorted((await session.execute(select(RepeatRefresh.status))).scalars().all())
    assert await read_committed(statuses) == ["done", "failed"]


async def test_the_running_flag_clears_even_when_cancelled(db_schema):
    async def hang(*a, **k):
        await asyncio.sleep(3600)
    task = asyncio.create_task(refresh.run(date(2026, 9, 1), date(2026, 9, 2), fetch_rows=hang))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert refresh.STATE["running"] is False


async def test_a_second_run_while_one_is_running_is_refused(db_schema):
    refresh.STATE["running"] = True
    try:
        assert (await refresh.run(date(2026, 9, 1), date(2026, 9, 2)))["error"] == "already running"
    finally:
        refresh.STATE["running"] = False


async def test_incremental_asks_for_the_last_7_ist_days(monkeypatch, db_schema):
    from app import ist
    from app.database import async_session
    from app.repeat import repository
    end = ist.yesterday()
    async with async_session() as db:
        await repository.record_run(db, window_start=(end - timedelta(days=30)).isoformat(),
                                    window_end=(end - timedelta(days=1)).isoformat(), status="done")
    asked = []

    async def fake_run(start, end, **kw):
        asked.append((start, end))
        return {}
    monkeypatch.setattr(refresh, "run", fake_run)
    await refresh.run_incremental()
    assert asked == [(end - timedelta(days=6), end)]


# "fetch last 90 days only, and then keep adding one day per day"
END = date(2026, 10, 7)


def test_a_routine_night_adds_the_new_day_with_a_one_week_re_read():
    assert refresh.incremental_start(END - timedelta(days=1), END) == END - timedelta(days=6)
    assert refresh.incremental_start(END, END) == END - timedelta(days=6)


def test_missed_nights_are_filled_from_the_first_missing_day():
    assert refresh.incremental_start(END - timedelta(days=12), END) == END - timedelta(days=11)


def test_nothing_older_than_the_screen_needs_is_ever_fetched():
    oldest = END - timedelta(days=refresh.HISTORY_DAYS - 1)
    assert refresh.incremental_start(None, END) == oldest
    assert refresh.incremental_start(END - timedelta(days=900), END) == oldest
    # The 90-day column: a 30-day cohort ending 90 days before as_of, looking 90 days further back.
    assert refresh.HISTORY_DAYS >= 90 + 30 + 90
