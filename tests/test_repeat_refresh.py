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


async def test_incremental_asks_for_the_last_7_ist_days(monkeypatch):
    from app import ist
    asked = []

    async def fake_run(start, end, **kw):
        asked.append((start, end))
        return {}
    monkeypatch.setattr(refresh, "run", fake_run)
    await refresh.run_incremental()
    end = ist.yesterday()
    assert asked == [(end - timedelta(days=6), end)]
