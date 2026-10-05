"""The ads refresh fetches only what is MISSING, per ad product.

Reported as *"the refresh is taking too much time"* against a screenshot reading "Sponsored Brands is
missing 2 of these days" — while the button re-downloaded Sponsored Products for the whole window
(~7 minutes) although SP already held every day. And the nightly job re-fetched all 60 days, two
reports per ad type, so Amazon refused the second Sponsored Brands report on alternate nights
(production runs 30, 32, 34, 36, 39, 40). Both came from asking for days already held.
"""
from datetime import date, timedelta

import pytest

from app.ads import refresh, repository

pytestmark = pytest.mark.regression

END = date(2026, 10, 4)


def _days(start: date, end: date) -> set[str]:
    return {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}


def _window(n: int) -> tuple[str, str]:
    return (END - timedelta(days=n - 1)).isoformat(), END.isoformat()


def test_THE_REPORTED_CASE_only_the_two_missing_SB_days_are_fetched():
    start, end = _window(7)
    held = {"sp": _days(END - timedelta(days=60), END), "sb": _days(END - timedelta(days=60), END - timedelta(days=2))}
    plan = refresh.plan_ranges(start, end, held, freshen_days=refresh.MANUAL_FRESHEN_DAYS)
    assert plan["sp"] is None, "Sponsored Products already holds the window and was re-downloaded"
    assert plan["sb"] == ("2026-10-03", "2026-10-04")


def test_the_nightly_run_fetches_the_settling_tail_not_all_60_days():
    start, end = _window(60)
    full = _days(END - timedelta(days=59), END - timedelta(days=1))   # held up to yesterday-of-end
    plan = refresh.plan_ranges(start, end, {"sp": full, "sb": full},
                               settle_days=refresh.NIGHTLY_SETTLE_DAYS)
    expected_start = (END - timedelta(days=refresh.NIGHTLY_SETTLE_DAYS - 1)).isoformat()
    assert plan == {"sp": (expected_start, end), "sb": (expected_start, end)}
    # One report per ad type: Amazon caps a report at 31 days.
    assert refresh.NIGHTLY_SETTLE_DAYS <= 31
    # ...and long enough to cover the 14-day attribution window, or late sales are never picked up.
    assert refresh.NIGHTLY_SETTLE_DAYS >= 14


def test_an_OLD_gap_extends_the_range_back_to_it_so_it_heals():
    start, end = _window(60)
    sb = _days(END - timedelta(days=59), END) - {(END - timedelta(days=30)).isoformat()}
    plan = refresh.plan_ranges(start, end, {"sp": _days(END - timedelta(days=59), END), "sb": sb},
                               settle_days=refresh.NIGHTLY_SETTLE_DAYS)
    assert plan["sb"][0] == (END - timedelta(days=30)).isoformat()
    assert plan["sp"][0] == (END - timedelta(days=refresh.NIGHTLY_SETTLE_DAYS - 1)).isoformat()


def test_a_complete_window_freshens_SP_only_never_SB():
    """SB is the report Amazon rations to a few a day; a manual press must not spend one on days
    already held."""
    start, end = _window(7)
    full = _days(END - timedelta(days=60), END)
    plan = refresh.plan_ranges(start, end, {"sp": full, "sb": full},
                               freshen_days=refresh.MANUAL_FRESHEN_DAYS)
    assert plan["sb"] is None
    assert plan["sp"] == ((END - timedelta(days=refresh.MANUAL_FRESHEN_DAYS - 1)).isoformat(), end)


def test_freshening_does_not_apply_when_something_IS_missing():
    start, end = _window(7)
    full = _days(END - timedelta(days=60), END)
    plan = refresh.plan_ranges(start, end, {"sp": full, "sb": full - {end}},
                               freshen_days=refresh.MANUAL_FRESHEN_DAYS)
    assert plan == {"sp": None, "sb": (end, end)}


def test_an_empty_store_fetches_the_whole_window_for_both():
    start, end = _window(7)
    assert refresh.plan_ranges(start, end, {}) == {"sp": (start, end), "sb": (start, end)}


# ─── Through run() ───────────────────────────────────────────────────────────


def _fake_amazon(monkeypatch, calls):
    from app.ads import reports, spapi_ads

    async def campaigns(client, **kw):
        return []

    async def groups(client, **kw):
        return []

    async def fetch_targeting(start, end, *, ad_product="sp", on_chunk=None, **kw):
        calls.append((ad_product, start, end))
        if on_chunk:
            await on_chunk([], start, end)
        return []

    monkeypatch.setattr(spapi_ads, "fetch_campaigns", campaigns)
    monkeypatch.setattr(spapi_ads, "fetch_sb_campaigns", campaigns)
    monkeypatch.setattr(spapi_ads, "fetch_ad_groups", groups)
    monkeypatch.setattr(spapi_ads, "fetch_sb_ad_groups", groups)
    monkeypatch.setattr(reports, "fetch_targeting", fetch_targeting)


@pytest.fixture
def configured(monkeypatch):
    from app import config

    class S:
        ads_configured = True
        ads_timeout = 5

    monkeypatch.setattr(refresh, "get_settings", lambda: S())
    refresh.reset_state()
    yield
    refresh.reset_state()


async def test_run_asks_Amazon_for_ONLY_the_missing_SB_days(monkeypatch, db_schema, configured):
    calls = []
    _fake_amazon(monkeypatch, calls)
    start, end = _window(7)

    async def held(_factory):
        return {"sp": _days(END - timedelta(days=60), END),
                "sb": _days(END - timedelta(days=60), END - timedelta(days=2))}

    monkeypatch.setattr(refresh, "_held_days", held)
    result = await refresh.run(start=start, end=end, freshen_days=refresh.MANUAL_FRESHEN_DAYS)
    assert calls == [("sb", "2026-10-03", "2026-10-04")], calls
    assert result["fetched"] == {"sp": None, "sb": ["2026-10-03", "2026-10-04"]}
    assert result["phase"] == "done"


async def test_the_gap_fill_makes_NO_Amazon_call_when_nothing_is_missing(monkeypatch, db_schema, configured):
    from app.ads import spapi_ads

    async def must_not_run(*a, **kw):
        raise AssertionError("the gap-fill called Amazon although nothing was missing")

    monkeypatch.setattr(spapi_ads, "fetch_campaigns", must_not_run)
    full = _days(END - timedelta(days=70), END)

    async def held(_factory):
        return {"sp": full, "sb": full}

    monkeypatch.setattr(refresh, "_held_days", held)
    result = await refresh.run(days=60, only_if_missing=True, today=END + timedelta(days=1))
    assert result.get("skipped") is True


async def test_the_gap_fill_DOES_fetch_a_throttled_SB_morning(monkeypatch, db_schema, configured):
    calls = []
    _fake_amazon(monkeypatch, calls)

    async def held(_factory):
        return {"sp": _days(END - timedelta(days=70), END),
                "sb": _days(END - timedelta(days=70), END - timedelta(days=1))}

    monkeypatch.setattr(refresh, "_held_days", held)
    await refresh.run(days=60, only_if_missing=True, today=END + timedelta(days=1))
    assert calls == [("sb", END.isoformat(), END.isoformat())]


def test_the_scheduler_passes_the_settling_tail_and_registers_the_gap_fill():
    """Source-level: the nightly job must pass `settle_days`, or it silently goes back to fetching
    only gaps and never re-reads a settling day's late sales."""
    import inspect

    from app import scheduler

    nightly = inspect.getsource(scheduler.scheduled_ads_refresh)
    assert "settle_days=ads_refresh.NIGHTLY_SETTLE_DAYS" in nightly
    retry = inspect.getsource(scheduler.scheduled_ads_retry)
    assert "only_if_missing=True" in retry
    assert scheduler.ADS_RETRY_IST > scheduler.ADS_REFRESH_IST, "the gap-fill must run AFTER the morning job"


def test_the_manual_refresh_route_freshens_rather_than_refetching_the_window():
    import inspect

    from app.routers import ads as ads_router

    source = inspect.getsource(ads_router.start_refresh)
    assert "freshen_days=refresh.MANUAL_FRESHEN_DAYS" in source
