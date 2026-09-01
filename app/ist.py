"""India Standard Time — the ONE place the offset lives, and the one place "today" is decided.

**This module exists because the same bug has been found SIX times in this codebase.** Every
instance had its own passing test, and the fix for each was scoped to the file that broke, which is
precisely why there was a seventh place to break. The list, from CLAUDE.md:

1. Orders tab: `new Date("2026-08-25")` is UTC midnight by spec, so IST rendered a ship-by date as
   05:30 the following morning — five and a half hours into the wrong day, in the column the
   warehouse plans against.
2. Ads tab: `toISOString()` in `maxDate()`/`presetRange()` offered a maximum date a day early
   between 00:00 and 05:30 IST.
3. Portfolio tab: the same `toISOString()`.
4. **The GST invoice date**: `invoice.html` set the date from `new Date().toISOString()`, so an
   invoice raised at 00:39 IST on 29 Aug read **2026-08-28** — a tax document in a legally
   sequential series, dated before the shipment it bills.
5. The ads once-per-day bid guard: the ledger stores `datetime.utcnow()`, so a UTC-day comparison
   would not hold for 5.5 hours out of every 24. That is `ads.logic.ist_day`, which now delegates
   here.
6. **The nightly schedule**: `CronTrigger(hour=3)` with no timezone on a UTC box fires at 08:30 IST,
   under a comment that read *"03:20 IST-ish (the box is UTC, so this is wall-clock server time)"* —
   the misreading written down as a fact, saying IST and meaning UTC in one sentence. The ads
   refresh was running at 09:20 IST, in the middle of the working morning.

The business runs in IST. The server clock is UTC (measured: `timedelta` reports
`Local time: ... UTC` on the production box) and `datetime.utcnow()`/`date.today()` therefore
answer a question nobody asked. Anything that decides *which day it is* must come through here.

**A fixed offset, not a tzdata lookup.** India has no DST and has not changed offset since 1945, so
`ZoneInfo("Asia/Kolkata")` would add a dependency on tzdata being present on the box for no
behavioural difference — and a missing tzdata fails at runtime, in the scheduler, at 2am.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: India Standard Time: UTC+05:30, no DST, ever.
IST = timezone(timedelta(hours=5, minutes=30))

#: The same offset as a bare timedelta, for arithmetic on naive UTC values read back from
#: SQLAlchemy (which drops tzinfo, so a stored timestamp comes back naive even though it is UTC).
IST_OFFSET = timedelta(hours=5, minutes=30)


def now() -> datetime:
    """The current instant as a timezone-aware IST datetime."""
    return datetime.now(IST)


def today() -> date:
    """**The IST calendar date — what "today" means to this business.**

    Never `date.today()`, which on the production box is the UTC date and is therefore a day behind
    between 00:00 and 05:30 IST. That window matters more than its size suggests: it is when the
    day's last invoice is raised and when the nightly jobs run.
    """
    return now().date()


def yesterday() -> date:
    """The last COMPLETE IST day.

    The ads and portfolio windows end here rather than today, because an ad charge lands hours after
    the click it belongs to — a window including today reads a punishing ACOS every morning that
    settles by evening, and a bid rule acting on that would cut bids on a measurement artefact.
    """
    return today() - timedelta(days=1)


def to_ist(value: datetime | None) -> datetime | None:
    """A UTC timestamp as IST. `None` passes through.

    **A naive value is treated as UTC**, because that is what it is: SQLAlchemy's `DateTime` drops
    the tzinfo, so a row read back from the database has none even though it was stored as UTC.
    Assuming local instead would subtract 5.5 hours from every deadline — a uniform error, and
    therefore the hardest kind to notice.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def day_of(when: datetime | None) -> str:
    """The IST calendar date of a naive UTC datetime, as ``YYYY-MM-DD``. ``""`` for None.

    This is what `ads.logic.ist_day` calls. "Not twice on the same day" is a decision taken in IST
    while the ledger records UTC: a bid change applied at 04:00 IST is 22:30 UTC the PREVIOUS day,
    so a UTC-day comparison would call it yesterday and allow a second run that morning.
    """
    if when is None:
        return ""
    converted = to_ist(when)
    return converted.date().isoformat() if converted else ""


def utc_hhmm(hour: int, minute: int = 0) -> tuple[int, int]:
    """An IST wall-clock time as the ``(hour, minute)`` a UTC-clocked cron needs.

    **This is the whole fix for defect 6.** APScheduler's `CronTrigger` takes no timezone here and
    evaluates in the server's local time, which is UTC on this box — so a job written as `hour=3`
    fires at 08:30 IST. Stating the time in IST and converting through one named function is the
    only form a reader cannot misread, and it is testable: ``utc_hhmm(8, 0) == (2, 30)``.

    Wraps around midnight, which is the case that makes hand-conversion error-prone — 02:00 IST is
    20:30 UTC the *previous* day, and the hour alone is what cron needs.

    Considered and rejected: `CronTrigger(timezone="Asia/Kolkata")`. It reads well but needs tzdata
    present on the box, and a missing tzdata fails inside the scheduler rather than at import.
    """
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{hour:02d}:{minute:02d} is not a wall-clock time")
    # Anchored on an arbitrary date: only the time-of-day travels, and the date wrap is discarded
    # because cron has no opinion about which day it is.
    anchored = datetime(2000, 1, 2, hour, minute, tzinfo=IST)
    as_utc = anchored.astimezone(timezone.utc)
    return as_utc.hour, as_utc.minute


def label(hour: int, minute: int = 0) -> str:
    """``"08:00 IST (02:30 UTC)"`` — for the startup log.

    Both times, deliberately. The IST one is what was intended and the UTC one is what `journalctl`
    timestamps will show, so a log line proves which was meant instead of leaving the next reader to
    redo the arithmetic that went wrong in the first place.
    """
    utc_hour, utc_minute = utc_hhmm(hour, minute)
    return f"{hour:02d}:{minute:02d} IST ({utc_hour:02d}:{utc_minute:02d} UTC)"
