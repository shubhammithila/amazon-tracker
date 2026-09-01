"""One timezone, tested at the boundary that has broken six times.

Every assertion here is about the 18:30Z seam — 00:00 IST — because that is where every one of the
six historical bugs lived, and where none of them was noticed: it is 5.5 hours out of 24, and the
hours nobody is looking at the screen.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app import ist


# ─── The offset itself ───────────────────────────────────────────────────────


def test_the_offset_is_five_and_a_half_hours_with_no_dst():
    """A fixed offset rather than a tzdata lookup, so a box without tzdata cannot fail at 2am."""
    assert ist.IST.utcoffset(None) == timedelta(hours=5, minutes=30)
    assert ist.IST_OFFSET == timedelta(hours=5, minutes=30)
    # No DST means the offset is the same in January and in July.
    assert ist.IST.utcoffset(datetime(2026, 1, 15)) == ist.IST.utcoffset(datetime(2026, 7, 15))


# ─── "Today", which is the decision ──────────────────────────────────────────


def test_today_is_the_ist_day_not_the_utc_day(monkeypatch):
    """**The five-and-a-half-hour window where `date.today()` is a day behind.**

    22:30 UTC on 31 Aug is 04:00 IST on 1 Sep. A UTC "today" calls that 31 Aug, so the nightly
    window, the presets and a GST invoice date would all be a day early — which is exactly what
    happened to invoice ST/26-27/0xx, dated before the shipment it billed.

    **The fake clock replaces `utcnow` and `today` as well as `now`, and a mutation is why.**
    Patching `now` alone left `ist.today()` free to call `datetime.utcnow().date()` and still pass:
    the whole module was stubbed except the one path the bug takes. Every way of asking this fake for
    the date must answer 31 Aug in UTC terms, so only an IST conversion can produce 1 Sep.
    """
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 8, 31, 22, 30, tzinfo=timezone.utc)
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return datetime(2026, 8, 31, 22, 30)

        @classmethod
        def today(cls):
            return datetime(2026, 8, 31, 22, 30)

    monkeypatch.setattr(ist, "datetime", _Fixed)
    monkeypatch.setattr(ist, "date", type("_D", (date,), {
        "today": classmethod(lambda cls: date(2026, 8, 31)),
    }))
    assert ist.today() == date(2026, 9, 1), (
        "the UTC date leaked through as 'today' — 22:30 UTC on the 31st is 04:00 IST on the 1st"
    )
    assert ist.yesterday() == date(2026, 8, 31)


def test_the_last_minute_of_an_ist_day_is_still_that_day(monkeypatch):
    """18:29Z is 23:59 IST — the same day. 18:30Z is the next. One minute decides it."""
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 8, 31, 18, 29, tzinfo=timezone.utc)
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(ist, "datetime", _Fixed)
    assert ist.today() == date(2026, 8, 31)


# ─── A stored UTC timestamp as its IST day ───────────────────────────────────


def test_day_of_reads_a_naive_value_as_utc():
    """SQLAlchemy drops tzinfo, so a stored timestamp comes back naive — and it IS UTC.

    Treating it as local would subtract 5.5 hours from every value, a uniform error and therefore
    the hardest kind to see.
    """
    assert ist.day_of(datetime(2026, 8, 30, 22, 30)) == "2026-08-31"
    assert ist.day_of(datetime(2026, 8, 31, 18, 29)) == "2026-08-31"
    assert ist.day_of(datetime(2026, 8, 31, 18, 30)) == "2026-09-01"
    assert ist.day_of(None) == ""


def test_day_of_accepts_an_already_aware_value():
    """A tz-aware value must not be shifted twice."""
    aware = datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc)
    assert ist.day_of(aware) == "2026-09-01"
    already_ist = datetime(2026, 9, 1, 0, 0, tzinfo=ist.IST)
    assert ist.day_of(already_ist) == "2026-09-01"


def test_the_ads_window_asks_ist_for_its_yesterday(monkeypatch):
    """**The mutation that escaped the first version of this file.**

    `test_today_is_the_ist_day_not_the_utc_day` patches `ist.datetime`, so anything that reaches
    `datetime.utcnow()` or `date.today()` DIRECTLY sails past it — which is exactly what
    `default_window` used to do, and what a careless edit would restore. Both survivors of the first
    mutation run were this hole.

    Patched at the seam that matters instead: `ist.today` itself. A `default_window` that does not
    call it cannot see the patched value, so the assertion fails.

    **The patched date is deliberately far from the real one, and that took a second attempt.** The
    first version used a fixed 2026-09-01 — which happened to BE the real date the day it was
    written, so `date.today()` and the patched `ist.today()` agreed and the mutation passed. Derived
    from the real today here, so no calendar day can make this test vacuous. The same fixture mistake
    as testing a per-campaign total with only one campaign.

    The window is what the nightly job fetches AND what "last 7 days" means on screen, so a UTC day
    here puts the stored coverage and the presets one day apart for 5.5 hours out of every 24.
    """
    from datetime import date, timedelta

    from app.ads import refresh

    # A year in the past: `date.today()` cannot coincide with it on any run.
    pretend = date.today() - timedelta(days=365)
    monkeypatch.setattr(ist, "today", lambda: pretend)

    start, end = refresh.default_window(7)
    assert end == (pretend - timedelta(days=1)).isoformat(), (
        f"default_window ended {end} rather than the day before {pretend} — it is not asking "
        f"`ist.today()`, so on the UTC-clocked box it fetches a day early every night between "
        f"00:00 and 05:30 IST"
    )
    assert start == (pretend - timedelta(days=7)).isoformat()


def test_the_ads_window_validator_asks_ist_for_today(monkeypatch):
    """The mirror image: the validator refusing the owner's real today as "in the future".

    Same seam, same reason — patched on `ist.today` so a direct `date.today()` cannot pass, and
    derived from the real today so the two can never coincide.
    """
    from datetime import date, timedelta

    from app.routers import ads as ads_router

    pretend = date.today() - timedelta(days=365)
    monkeypatch.setattr(ist, "today", lambda: pretend)

    # The IST today must be accepted — refusing it as "in the future" is the mirror-image bug.
    week_before = (pretend - timedelta(days=6)).isoformat()
    assert ads_router._window(week_before, pretend.isoformat(), None) == (
        week_before, pretend.isoformat()
    )
    # And tomorrow is still a mistake rather than a caveat.
    with pytest.raises(ValueError, match="future"):
        ads_router._window(week_before, (pretend + timedelta(days=1)).isoformat(), None)


def test_the_ads_guard_delegates_here_rather_than_keeping_its_own_offset():
    """`ads.logic.ist_day` keeps its name and its docstring — it is called in four places and the
    docstring records why the bid guard needs an IST day. What it must not keep is a SECOND copy of
    the offset, which is how a codebase ends up with three.
    """
    from app.ads import logic

    for moment in (datetime(2026, 8, 30, 22, 30), datetime(2026, 8, 31, 18, 29),
                   datetime(2026, 8, 31, 18, 30), None):
        assert logic.ist_day(moment) == ist.day_of(moment)


def test_the_orders_tab_shares_the_same_offset_object():
    """`orders.logic.IST` predates this module and was already correct. It must not be a rival
    definition of the same fact — three copies of an offset is how they drift.
    """
    from app.orders import logic as orders_logic

    assert orders_logic.IST is ist.IST


# ─── The cron conversion, which is defect 6 ──────────────────────────────────


def test_an_ist_wall_clock_time_becomes_the_utc_one_cron_needs():
    """**08:00 IST is 02:30 UTC.** The whole of defect 6 is this subtraction not being made.

    Asserted on the IST input rather than on the UTC output, because a test written as
    `ADS_REFRESH_HOUR == 2` would pin the arithmetic rather than the intent.
    """
    assert ist.utc_hhmm(8, 0) == (2, 30)
    assert ist.utc_hhmm(7, 30) == (2, 0)
    assert ist.utc_hhmm(3, 50) == (22, 20), "the OLD value: 03:50 IST is 22:20 UTC the day before"


def test_the_conversion_wraps_around_midnight():
    """The case that makes hand-conversion go wrong: 02:00 IST is 20:30 UTC the PREVIOUS day, and
    an implementation that clamps rather than wraps would silently schedule a different time."""
    assert ist.utc_hhmm(2, 0) == (20, 30)
    assert ist.utc_hhmm(0, 0) == (18, 30)
    assert ist.utc_hhmm(5, 29) == (23, 59)
    assert ist.utc_hhmm(5, 30) == (0, 0)


def test_a_time_that_is_not_a_time_is_refused():
    """Rather than producing a plausible hour from nonsense."""
    for bad in ((24, 0), (-1, 0), (8, 60), (8, -1)):
        with pytest.raises(ValueError):
            ist.utc_hhmm(*bad)


def test_the_startup_label_states_both_times():
    """The log line has to prove which was meant. `journalctl` stamps UTC, the intent is IST, and
    printing only one leaves the next reader to redo the arithmetic that went wrong here."""
    assert ist.label(8, 0) == "08:00 IST (02:30 UTC)"
    assert "IST" in ist.label(7, 30) and "UTC" in ist.label(7, 30)


# ─── Round-trip ──────────────────────────────────────────────────────────────


def test_to_ist_and_day_of_agree_across_the_whole_day():
    """A property over all 1,440 minutes, so no single hour can be special-cased into passing."""
    start = datetime(2026, 8, 31, 0, 0)
    for minute in range(0, 1440):
        moment = start + timedelta(minutes=minute)
        assert ist.day_of(moment) == ist.to_ist(moment).date().isoformat()
