"""The background economics refresh, and the progress the screen polls.

**Never awaited inside a request.** A Data Kiosk query takes one to two minutes (measured), so
a route that awaited it would hold the connection open and time out behind Caddy. The route
starts this with ``asyncio.create_task`` and returns at once; the screen polls
``/portfolio/refresh-status``.

Deliberately the same shape as ``app.orders.refresh``: a module-level ``STATE``, a monotonic
percentage, ``status()`` that converts datetimes to ISO strings, and a concurrency guard that
REFUSES rather than raising. Two progress implementations that differ in their details are two
things to learn, and the orders one has already had its bugs found on production.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from app import ist
from app.database import async_session
from app.portfolio import ads, economics, repository
from app.shipment.spapi import SpApiError, SpApiNotConfigured

#: The most days one incremental run will fetch. Normally there is exactly ONE missing day, so this
#: only bites after an outage — and then it bounds the run rather than letting a fortnight's gap hold
#: the job open for hours on a 951 MB box. Later runs pick up the rest, and `range_completeness`
#: refuses any range still touching a gap, so a partial catch-up is visible rather than quietly
#: summing short.
#:
#: 7 because Amazon caps ONE ads report at 31 days (`ads.MAX_REPORT_DAYS`), so a wider span would
#: silently become several reports and multiply a "catch-up" run's cost.
MAX_BACKFILL_DAYS = 7

logger = logging.getLogger(__name__)

#: Live progress for the banner. Module-level because there is exactly one refresh at a time
#: and the screen polls a separate request that must see it.
STATE: dict = {}


def reset_state() -> None:
    """Back to idle. Called at import and by tests."""
    STATE.clear()
    STATE.update({
        "running": False,
        "started_at": None,
        "finished_at": None,
        # "econ_submit" | "econ_poll" | "econ_download" | "econ_store"
        # | "ads_report" | "ads_store" | "done" | "failed"
        "phase": "idle",
        "percent": 0,
        "rows": 0,
        "sku_rows": 0,
        "ads_rows": 0,
        "window_start": None,
        "window_end": None,
        "error": None,
        #: An ads failure that did NOT cost the margins. Reported separately from `error` so the
        #: screen can say "margins are current, ACOS is stale" rather than "the refresh failed".
        "ads_error": None,
        "refused": False,
    })


reset_state()

#: How the bar is divided across TWO APIs. **Uneven because the phases are, by an order of
#: magnitude.** Measured: the economics query reaches DONE in ~30 seconds, while the advertising
#: report takes **15-25 MINUTES** to generate (measured from Amazon's own timestamps: 18.5
#: minutes for a six-day window). So the ad report owns more than half the bar — it is where the
#: owner is actually waiting, and a bar sitting at 40% for twenty minutes would read as hung.
#:
#: The honest part: **no phase here has a true denominator.** Neither API publishes progress for
#: a running query, so each poll phase is `attempt / POLL_MAX` — a fraction of a CEILING usually
#: not reached, which makes the bar deliberately pessimistic. It reaches 100 only when rows are
#: stored, so a bar jumping from 60 to 100 is expected rather than a fault.
PHASE_BOUNDS = {
    "econ_submit": (0, 5),
    "econ_poll": (5, 26),
    "econ_download": (26, 30),
    "econ_store": (30, 36),
    "ads_report": (36, 92),
    "ads_store": (92, 100),
}

#: Human labels for the bar. Kept here rather than in the template so a new phase cannot appear
#: on screen as a raw key.
PHASE_LABELS = {
    "econ_submit": "Asking Amazon for the economics…",
    "econ_poll": "Amazon is preparing the economics — about a minute…",
    "econ_download": "Downloading the economics…",
    "econ_store": "Storing the margins…",
    # Windows over 31 days are several reports (Amazon's cap), so the wording covers both: a 30d
    # refresh is one report, 90d is three run in sequence and the wait multiplies.
    "ads_report": "Amazon is generating the advertising report — 15-25 minutes per 31 days…",
    "ads_store": "Storing the ad figures…",
}


def _set_percent(value: float) -> None:
    """Publish a whole-number percent that never goes DOWN.

    Monotonic for the same reason the orders bar is: a bar that steps backwards reads as a
    fault. Here it matters because the poll phase is a fraction of a ceiling — a query that
    finishes on attempt 3 of 40 jumps from 15% to the download phase, and nothing may later
    report a lower number.
    """
    percent = max(0, min(100, int(value)))
    if percent > int(STATE.get("percent") or 0):
        STATE["percent"] = percent


def _progress(phase: str, done: int, total: int) -> None:
    """Map one phase's (done, total) onto its slice of the bar."""
    STATE["phase"] = phase
    low, high = PHASE_BOUNDS.get(phase, (0, 100))
    fraction = (done / total) if total else 0
    _set_percent(low + (high - low) * fraction)


def status() -> dict:
    """A copy of the current progress, JSON-safe.

    **Datetimes become ISO strings HERE, not in each caller.** `JSONResponse` cannot serialise a
    datetime, and the orders feature shipped exactly that bug — on the "already running" path,
    which is the one path that fires when someone is trying to find out what is happening.
    """
    snapshot = dict(STATE)
    for field in ("started_at", "finished_at"):
        value = snapshot.get(field)
        if isinstance(value, datetime):
            snapshot[field] = value.isoformat()
    return snapshot


async def run(
    db_factory=async_session,
    *,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    sleep=asyncio.sleep,
) -> dict:
    """Fetch one window from BOTH Amazon APIs and store it. Returns a status snapshot.

    **Refuses rather than raises when one is already running**, so the nightly job overlapping a
    manual refresh is a no-op instead of an error in the log every night. The guard lives here
    rather than in the route, so every caller inherits it.

    Two APIs, in this order and for this reason: **the economics are stored BEFORE the ad report
    is even requested.** The margins are the load-bearing half of this dashboard and take 30
    seconds; the ad report takes 15-25 minutes and can fail on its own. Storing first means an
    ads failure leaves a fully useful tab with stale ACOS, rather than costing the margins too.

    ``start``/``end`` request an explicit window (the date picker); ``days`` requests the last N
    days ending yesterday. ``sleep`` is injectable so a test drives the whole sequence without
    spending twenty minutes.
    """
    if STATE.get("running"):
        logger.info("portfolio refresh: already running, refused")
        snapshot = status()
        snapshot["refused"] = True
        return snapshot

    reset_state()
    started = datetime.utcnow()
    STATE.update({"running": True, "started_at": started, "phase": "econ_submit"})

    window_start = window_end = None
    try:
        # ── Phase 1-4: the economics (margins, fees, TACOS) ──
        rows, sku_rows, window_start, window_end = await economics.fetch_economics(
            days=days or economics.WINDOW_DAYS,
            start=start, end=end,
            sleep=sleep, on_progress=_progress,
        )
        STATE.update({"window_start": window_start, "window_end": window_end})

        _progress("econ_store", 0, 2)
        async with db_factory() as db:
            stored = await repository.save_economics_daily(db, rows)
            _progress("econ_store", 1, 2)
            stored_skus = await repository.save_sku_snapshot(db, sku_rows)
        _progress("econ_store", 2, 2)
        STATE.update({"rows": stored, "sku_rows": stored_skus})
        logger.info(
            "portfolio refresh: %d economics row(s) + %d per-SKU row(s) for %s..%s",
            stored, stored_skus, window_start, window_end,
        )

        # ── Phase 5-6: the advertising report (ACOS) ──
        #
        # In its own try, so a failure here cannot reach the outer handler and mark the whole
        # refresh failed — the margins above are already committed and current.
        ads_stored = 0
        try:
            def ads_progress(done, total):
                _progress("ads_report", done, total)

            ad_rows = await ads.fetch_acos(
                window_start, window_end, sleep=sleep, on_progress=ads_progress
            )
            _progress("ads_store", 0, 1)
            async with db_factory() as db:
                ads_stored = await repository.save_ads_daily(db, ad_rows)
            _progress("ads_store", 1, 1)
            STATE["ads_rows"] = ads_stored
            logger.info("portfolio refresh: %d ad row(s) stored", ads_stored)
        except ads.AdsNotConfigured:
            # Expected on any install without advertising keys. Not an error: the tab shipped
            # before ACOS existed and says "not configured" rather than failing.
            STATE["ads_error"] = (
                "Advertising credentials are not configured, so ACOS is unavailable."
            )
            logger.info("portfolio refresh: ACOS skipped, advertising is not configured")
        except ads.AdsError as exc:
            STATE["ads_error"] = str(exc)
            logger.warning("portfolio refresh: the ad report failed: %s", exc)

        async with db_factory() as db:
            await repository.record_refresh(
                db, window_start=window_start, window_end=window_end,
                rows_stored=stored, error=STATE.get("ads_error"), started_at=started,
            )

        STATE.update({"phase": "done", "percent": 100})
    except SpApiNotConfigured:
        # Not an error worth a stack trace: the app is expected to work without Amazon
        # credentials, and the screen says so rather than the log filling up.
        STATE.update({"phase": "failed", "error": "Amazon credentials are not configured."})
        logger.info("portfolio refresh: skipped, SP-API is not configured")
    except SpApiError as exc:
        # Amazon's own message is surfaced verbatim — it is written for a developer and has been
        # the most useful thing at every step of this integration.
        STATE.update({"phase": "failed", "error": str(exc)})
        logger.warning("portfolio refresh failed: %s", exc)
        async with db_factory() as db:
            await repository.record_refresh(
                db, window_start=window_start, window_end=window_end,
                rows_stored=0, error=str(exc), started_at=started,
            )
    except Exception as exc:                    # noqa: BLE001 - a crash must not wedge the flag
        # Without this the `running` flag would stay True for the life of the process and every
        # later refresh — including the nightly one — would be refused. The orders refresh
        # carries the same guard for the same reason.
        STATE.update({"phase": "failed", "error": f"Unexpected failure: {exc}"})
        logger.exception("portfolio refresh crashed")
    finally:
        STATE["running"] = False
        STATE["finished_at"] = datetime.utcnow()
        # **The retention sweep runs here, not on the success path.** A purge that only happens
        # when a fetch succeeds is a side effect rather than a policy: a week of failed ad reports
        # would leave the fastest-growing tables in this feature unpruned, which is exactly how
        # `economics_snapshot` reached 32 windows with no retention at all. The Ads tab's own
        # nightly sweep is duplicated for the same reason.
        #
        # Wrapped, because this block also runs on the crash path and a purge failure must not
        # replace the real error with its own.
        try:
            async with db_factory() as db:
                await repository.purge_daily(db)
        except Exception:                        # noqa: BLE001 - never mask the original failure
            logger.warning("portfolio refresh: the retention purge failed", exc_info=True)

    return status()


async def run_incremental(
    db_factory=async_session,
    *,
    max_days: int = MAX_BACKFILL_DAYS,
    today: date | None = None,
    sleep=asyncio.sleep,
) -> dict:
    """Fetch only the days NOT already held, newest-first-bounded. The nightly path.

    Normally that is exactly one day — yesterday — which is what makes the nightly cost ~15 min
    instead of the ~45 a rolling 90-day refetch would take on a box where the Ads tab's own job
    already runs for an hour.

    **Bounded at `max_days`**, so a long gap (the app was off for a fortnight) cannot hold the job
    open for hours. The remaining days are picked up by subsequent runs, and `range_completeness`
    refuses any range still touching a gap rather than summing short — so a partial catch-up is
    visible rather than quietly wrong.

    **A contiguous span is fetched, not a set of individual days.** Amazon bills a report per
    request, not per day, and one 3-day report costs the same as one 1-day report; asking for three
    separate days would triple the ~15 minutes for no benefit. Days already held inside the span are
    simply rewritten with the same figures, which `save_economics_daily` does by design.

    Returns the same status snapshot as `run`, with ``skipped=True`` when there was nothing to do.
    """
    end_day = (today or ist.today()) - timedelta(days=1)
    async with db_factory() as db:
        held = await repository.days_held(db)

    wanted = [
        (end_day - timedelta(days=offset)).isoformat()
        for offset in range(max_days)
    ]
    missing = sorted(day for day in wanted if day not in held)
    if not missing:
        logger.info("portfolio refresh: every day up to %s is held, nothing to fetch", end_day)
        snapshot = status()
        snapshot["skipped"] = True
        return snapshot

    logger.info(
        "portfolio refresh: %d day(s) missing, fetching %s..%s",
        len(missing), missing[0], missing[-1],
    )
    return await run(
        db_factory, start=missing[0], end=missing[-1], sleep=sleep,
    )
