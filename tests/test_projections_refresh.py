"""The weekly 7d/30d recompute. Reuses app.portfolio.economics — no new Amazon integration."""
import pytest

from app.projections import refresh, repository

pytestmark = pytest.mark.asyncio


async def _no_sleep(_seconds):
    return None


def _fake_catalogue_fn(groups_source):
    async def _fn():
        return groups_source, None, "sheet"
    return _fn


async def test_run_stores_a_blended_rate_from_two_fetched_windows(db, monkeypatch):
    """The whole point measured against the real account: 30d and 7d windows both fetched (or
    already stored), a parent's blended rate ends up between the two per-day rates."""
    sheet = {
        "B01": {"asin": "B01", "name": "Chana Sattu", "weight": 1.0, "brand": "Mithila Foods",
                "active": True},
    }
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _fake_catalogue_fn(sheet))

    def _econ_row(units):
        return {"childAsin": "B01", "sales": {"unitsOrdered": units, "netUnitsSold": units}}

    calls = []

    async def _fake_fetch_economics(*, days, sleep=None, **_kwargs):
        calls.append(days)
        if days == 30:
            return [_econ_row(300)], [], "2026-08-02", "2026-08-31"   # 10 kg/day
        return [_econ_row(140)], [], "2026-08-25", "2026-08-31"       # 20 kg/day

    monkeypatch.setattr("app.portfolio.economics.fetch_economics", _fake_fetch_economics)

    async def _fake_save_snapshot(db_, start, end, rows):
        return len(rows)

    monkeypatch.setattr("app.portfolio.repository.save_snapshot", _fake_save_snapshot)

    async def _fake_load_snapshot(db_, window):
        if window == ("2026-08-02", "2026-08-31"):
            return [_econ_row(300)]
        return [_econ_row(140)]

    monkeypatch.setattr("app.portfolio.repository.load_snapshot", _fake_load_snapshot)

    async def _fake_windows_available(db_, limit=12):
        return []  # nothing cached, so both windows are fetched

    monkeypatch.setattr("app.portfolio.repository.windows_available", _fake_windows_available)

    result = await refresh.run(db, sleep=_no_sleep)

    assert result["error"] is None
    assert sorted(calls) == [7, 30], "both windows must be requested"
    rows = await repository.load_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["thirty_day_rate"] == 10.0
    assert row["seven_day_rate"] == 20.0
    # 0.4*20 + 0.6*10 = 14.0, the default weight
    assert row["daily_rate"] == 14.0
    assert row["diverged"] is True


async def test_run_reuses_an_already_stored_window_without_refetching(db, monkeypatch):
    """`windows_available` says the 30-day window is already stored — the job must not spend a
    ~2-minute Data Kiosk query for data it already has.

    **`today` is pinned explicitly**, and this is not incidental: `_ensure_window` must check
    `economics.window_for(today, days)` against the cache BEFORE calling `fetch_economics` at
    all, which means it computes real dates from the real clock outside of any mocked function.
    Leaving `today` to default to the real `date.today()` would make the 30-day window this test
    expects to be "already cached" (2026-08-02..2026-08-31) match only on one specific real-world
    date and silently do the wrong thing — fetching unnecessarily — every other day the suite
    runs. Pinning `today` is what makes the cache-hit path exercised deterministically.
    """
    from datetime import date

    sheet = {"B01": {"asin": "B01", "name": "Chana Sattu", "weight": 1.0,
                     "brand": "Mithila Foods", "active": True}}
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _fake_catalogue_fn(sheet))

    async def _fake_windows_available(db_, limit=12):
        return [{"start": "2026-08-02", "end": "2026-08-31", "rows": 1}]

    monkeypatch.setattr("app.portfolio.repository.windows_available", _fake_windows_available)

    fetch_calls = []

    async def _fake_fetch_economics(*, days, sleep=None, **_kwargs):
        fetch_calls.append(days)
        return [], [], "2026-08-25", "2026-08-31"

    monkeypatch.setattr("app.portfolio.economics.fetch_economics", _fake_fetch_economics)

    async def _fake_save_snapshot(db_, start, end, rows):
        return len(rows)

    monkeypatch.setattr("app.portfolio.repository.save_snapshot", _fake_save_snapshot)

    async def _fake_load_snapshot(db_, window):
        return [{"childAsin": "B01", "sales": {"unitsOrdered": 30, "netUnitsSold": 30}}]

    monkeypatch.setattr("app.portfolio.repository.load_snapshot", _fake_load_snapshot)

    # 2026-09-01 -> window_for(_, 30) == ("2026-08-02", "2026-08-31"), matching the "already
    # cached" fixture above exactly; window_for(_, 7) == ("2026-08-25", "2026-08-31"), which the
    # fixture does NOT list as cached, so only that one must trigger fetch_economics.
    await refresh.run(db, sleep=_no_sleep, today=date(2026, 9, 1))
    assert fetch_calls == [7], "the 30-day window was refetched despite already being stored"


async def test_run_records_a_failed_fetch_without_touching_existing_rows(db, monkeypatch):
    """A failed or partial fetch must not overwrite good data — the same discipline the
    ads_refresh table enforces."""
    from app.projections import repository as proj_repo

    await proj_repo.save_row(db, "Chana Sattu", {"daily_rate": 5.0}, source="sheet")

    async def _fake_catalogue():
        return (
            {"B01": {"asin": "B01", "name": "Chana Sattu", "weight": 1.0,
                     "brand": "Mithila Foods", "active": True}},
            None, "sheet",
        )
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _fake_catalogue)

    async def _fake_windows_available(db_, limit=12):
        return []

    monkeypatch.setattr("app.portfolio.repository.windows_available", _fake_windows_available)

    from app.shipment.spapi import SpApiError

    async def _fake_fetch_economics(*, days, sleep=None, **_kwargs):
        raise SpApiError("Amazon credentials are not configured.")

    monkeypatch.setattr("app.portfolio.economics.fetch_economics", _fake_fetch_economics)

    result = await refresh.run(db, sleep=_no_sleep)

    assert result["error"] is not None
    rows = await proj_repo.load_rows(db)
    assert rows[0]["daily_rate"] == 5.0, "the failed fetch overwrote the previous good rate"

    last = await proj_repo.last_refresh(db)
    assert last["error"] is not None


async def test_run_reads_the_saved_blend_weight_not_the_hardcoded_default(db, monkeypatch):
    from app.projections import logic, repository as proj_repo

    await proj_repo.save_blend_settings(db, {"seven_day_weight": 0.8})

    sheet = {"B01": {"asin": "B01", "name": "Chana Sattu", "weight": 1.0,
                     "brand": "Mithila Foods", "active": True}}
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", _fake_catalogue_fn(sheet))

    async def _fake_windows_available(db_, limit=12):
        return []

    monkeypatch.setattr("app.portfolio.repository.windows_available", _fake_windows_available)

    def _econ_row(units):
        return {"childAsin": "B01", "sales": {"unitsOrdered": units, "netUnitsSold": units}}

    async def _fake_fetch_economics(*, days, sleep=None, **_kwargs):
        return ([_econ_row(300)], [], "2026-08-02", "2026-08-31") if days == 30 else \
               ([_econ_row(140)], [], "2026-08-25", "2026-08-31")

    monkeypatch.setattr("app.portfolio.economics.fetch_economics", _fake_fetch_economics)

    async def _fake_save_snapshot(db_, start, end, rows):
        return len(rows)
    monkeypatch.setattr("app.portfolio.repository.save_snapshot", _fake_save_snapshot)

    async def _fake_load_snapshot(db_, window):
        return [_econ_row(300)] if window[1].endswith("-31") and window[0].endswith("-02") \
            else [_econ_row(140)]
    monkeypatch.setattr("app.portfolio.repository.load_snapshot", _fake_load_snapshot)

    await refresh.run(db, sleep=_no_sleep)

    rows = await proj_repo.load_rows(db)
    # 0.8*20 + 0.2*10 = 18.0, using the SAVED 0.8 weight, not the DEFAULT_BLEND 0.4
    assert rows[0]["daily_rate"] == 18.0
