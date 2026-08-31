"""The background ads refresh, and the progress the screen polls.

**Never awaited inside a request.** The targeting report takes ~5.5 minutes to generate (measured,
7-day window), and a window over 31 days is several reports in sequence. A route that awaited it
would hold the connection open and time out behind Caddy.

Deliberately the same shape as `portfolio.refresh` and `orders.refresh`: a module-level `STATE`, a
monotonic percentage, `status()` converting datetimes to ISO strings, and a concurrency guard that
REFUSES rather than raising. Three progress implementations differing in their details would be
three things to learn, and the orders one has already had its bugs found on production.

**The entity phase runs FIRST and is cheap; the report phase is the wait.** Campaigns and ad groups
are 24 and 2,542 rows — seconds. Keywords and targets are 148,291 and 200,000+, so they are NOT
fetched wholesale: the tab only needs names for the hierarchy, and the bids it edits come from the
report plus a live re-read at apply time.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

import httpx

from app.ads import repository, reports, spapi_ads
from app.config import get_settings
from app.database import async_session
from app.portfolio.ads import AdsError, AdsNotConfigured

logger = logging.getLogger(__name__)

#: Live progress for the banner. Module-level because there is exactly one refresh at a time and the
#: screen polls a separate request that must see it.
STATE: dict = {}


def reset_state() -> None:
    """Back to idle. Called at import and by tests."""
    STATE.clear()
    STATE.update({
        "running": False,
        "started_at": None,
        "finished_at": None,
        # "campaigns" | "ad_groups" | "report" | "store" | "done" | "failed"
        "phase": "idle",
        "percent": 0,
        "campaigns": 0,
        "ad_groups": 0,
        "rows": 0,
        "daily_rows": 0,
        "sb_rows": 0,
        "purged": 0,
        #: An SB failure that did NOT cost the Sponsored Products data. Reported separately from
        #: `error` so the screen can say "SP is current, SB is stale" rather than "the refresh
        #: failed" — the same distinction the portfolio refresh draws for ACOS.
        "sb_error": None,
        "window_start": None,
        "window_end": None,
        "error": None,
        "refused": False,
    })


reset_state()

#: How the bar is divided. **Uneven because the phases are.** Campaigns and ad groups are seconds
#: (24 and 2,542 rows); the report is ~5.5 minutes per 31 days and is where the owner is actually
#: waiting, so it owns three quarters of the bar. A bar sitting at 15% for six minutes reads as hung.
PHASE_BOUNDS = {
    "campaigns": (0, 5),
    "ad_groups": (5, 15),
    "report": (15, 88),
    "store": (88, 100),
}

PHASE_LABELS = {
    "campaigns": "Reading your campaigns…",
    "ad_groups": "Reading ad groups…",
    # Names the per-31-day cost, because a 60- or 90-day window is 2-3 reports in sequence and the
    # wait multiplies. Being vague here makes a normal 15-minute wait look like a fault.
    "report": "Amazon is generating the performance report — about 6 minutes per 31 days…",
    "store": "Storing the figures…",
}


def _set_percent(value: float) -> None:
    """Publish a whole-number percent that never goes DOWN.

    Monotonic for the same reason the orders bar is: a bar that steps backwards reads as a fault.
    Here it matters because the poll phase is a fraction of a CEILING — a report that finishes on
    poll 3 of 135 jumps from 17% to the store phase, and nothing may later report a lower number.
    """
    current = STATE.get("percent") or 0
    STATE["percent"] = max(current, int(max(0, min(100, value))))


def _progress(phase: str, done: float, total: float) -> None:
    low, high = PHASE_BOUNDS.get(phase, (0, 100))
    STATE["phase"] = phase
    fraction = (done / total) if total else 0
    _set_percent(low + (high - low) * fraction)


def status() -> dict:
    """A JSON-safe copy for the poller. Datetimes to ISO strings HERE, once.

    `JSONResponse` cannot serialise a datetime, and this app has shipped that exact defect on the
    orders payload — found in a browser on production, not by a test.
    """
    out = dict(STATE)
    for field in ("started_at", "finished_at"):
        value = out.get(field)
        out[field] = value.isoformat() if isinstance(value, datetime) else value
    out["phase_label"] = PHASE_LABELS.get(out.get("phase"), "")
    return out


def default_window(days: int = 7, *, today: date | None = None) -> tuple[str, str]:
    """The window a refresh uses when none is given.

    **Ends YESTERDAY, never today.** Today's advertising figures are still settling — a click costs
    immediately while its attributed sale can land hours later — so a window including today shows a
    punishing ROAS every morning that recovers by evening. A bid rule acting on that would cut bids
    on a measurement artefact. Same rule the Portfolio tab documents.
    """
    end = (today or date.today()) - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


async def run(
    *,
    days: int = 7,
    start: str | None = None,
    end: str | None = None,
    sleep=None,
    db_factory=async_session,
    today: date | None = None,
) -> dict:
    """Refresh the entity cache and one window of performance. Returns the final `status()`.

    **Refuses rather than queues** when a refresh is already running: two concurrent report requests
    would double the wait and race on the same rows, and the screen can simply say so.

    Phase order matters. Campaigns and ad groups are stored FIRST because they are cheap and because
    the hierarchy is useful on its own — if the report then fails, the tab still renders its campaign
    list with a note, rather than being empty. Same isolation the portfolio refresh uses to keep
    margins when the ad report fails.
    """
    if STATE.get("running"):
        logger.info("ads refresh: already running, refusing")
        return {**status(), "refused": True}

    settings = get_settings()
    if not settings.ads_configured:
        # Not an error worth a stack trace: the app is expected to work without ads credentials.
        reset_state()
        STATE.update({
            "phase": "failed",
            "error": "Advertising credentials are not configured, so the Ads tab has no data.",
        })
        return status()

    window_start, window_end = (start, end) if (start and end) else default_window(days, today=today)

    reset_state()
    STATE.update({
        "running": True,
        "started_at": datetime.utcnow(),
        "phase": "campaigns",
        "window_start": window_start,
        "window_end": window_end,
    })

    try:
        async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
            # ── Campaigns ──
            _progress("campaigns", 0, 2)
            campaigns = await spapi_ads.fetch_campaigns(client)
            _progress("campaigns", 1, 2)

            # **Sponsored Brands is a separate API, and it was missing entirely until now.**
            # Measured: 6 SB campaigns, 66 ad groups, 4,939 keywords all with editable bids. An SB
            # failure must not cost the SP data, so it is caught and reported rather than raised —
            # the same isolation the portfolio refresh uses to keep margins when ACOS fails.
            try:
                campaigns += await spapi_ads.fetch_sb_campaigns(client)
            except AdsError as exc:
                STATE["sb_error"] = f"Sponsored Brands campaigns could not be read: {exc}"
                logger.warning("ads refresh: SB campaigns failed: %s", exc)

            async with db_factory() as db:
                await repository.save_entities(db, campaigns)
            STATE["campaigns"] = len(campaigns)
            _progress("campaigns", 2, 2)

            # ── Ad groups ──
            #
            # For the ENABLED campaigns only. 2,542 ad groups exist across all campaigns; fetching
            # those belonging to paused campaigns costs pages to render rows nothing can be bid on.
            _progress("ad_groups", 0, 2)
            sp_active = [c["entity_id"] for c in campaigns
                         if c.get("state") == "ENABLED" and c.get("ad_product", "sp") == "sp"]
            sb_active = [c["entity_id"] for c in campaigns
                         if c.get("state") == "ENABLED" and c.get("ad_product") == "sb"]
            ad_groups = await spapi_ads.fetch_ad_groups(client, campaign_ids=sp_active or None)
            _progress("ad_groups", 1, 2)
            if sb_active:
                try:
                    ad_groups += await spapi_ads.fetch_sb_ad_groups(
                        client, campaign_ids=sb_active
                    )
                except AdsError as exc:
                    STATE["sb_error"] = f"Sponsored Brands ad groups could not be read: {exc}"
                    logger.warning("ads refresh: SB ad groups failed: %s", exc)
            async with db_factory() as db:
                await repository.save_entities(db, ad_groups)
            STATE["ad_groups"] = len(ad_groups)
            _progress("ad_groups", 2, 2)

        # ── The report ──
        #
        # Outside the client above: `fetch_targeting` opens its own, because a 90-day window holds
        # the connection for three consecutive reports and a single long-lived client across all of
        # it is more likely to be dropped mid-poll.
        def on_report_progress(done, total):
            _progress("report", done, total)

        # **DAILY, not SUMMARY.** Per-day rows are what make an arbitrary sub-range instant: with
        # them, "I have 30 days, show me 20" is a GROUP BY rather than another 6-minute report.
        # Measured: DAILY returns 3.6x the rows (45,650 vs 12,854 for 7 days) and bulk-inserts at
        # 30,921 rows/sec, so 30 days stores in ~6 seconds.
        rows = await reports.fetch_targeting(
            window_start, window_end, daily=True,
            sleep=sleep, on_progress=on_report_progress,
        )

        # ── Store ──
        #
        # ONE grain: the per-day rows, which every window is summed from.
        #
        # There used to be a second, window-grain write here — and keeping both is what let two code
        # paths answer the same question differently. Sponsored Brands was written to the window
        # table and not to the daily one, so a window nobody had fetched exactly under-reported by
        # 28% of spend. Now every figure on the tab comes from these rows.
        _progress("store", 0, 2)
        async with db_factory() as db:
            daily_stored = await repository.save_daily(db, rows)
            _progress("store", 1, 2)
            # Keep the daily table bounded — production is at 87% disk and every deploy copies the
            # whole database.
            purged = await repository.purge_daily(db)
        STATE["rows"] = daily_stored
        STATE["daily_rows"] = daily_stored
        STATE["purged"] = purged

        # ── Sponsored Brands performance ──
        #
        # A SEPARATE report (`sbTargeting`) after the SP one, not in parallel: both throttle, and
        # `sbTargeting` in particular returns 429 until it is given room — measured, three immediate
        # creates all throttled while one after a 60-second pause succeeded.
        #
        # **DAILY, exactly like Sponsored Products — and this line is the bug that was fixed.**
        #
        # It used to store SB at the window grain ONLY, and the comment here justified it: "SB is
        # 2,914 rows against SP's 12,205, so the sub-range machinery matters far less there". That
        # reasoned from ROW COUNT. The figure that decides it is SPEND SHARE — **SB is 28% of the
        # money** — and the read side sums daily rows for any window nobody fetched exactly, where
        # SB had no rows at all.
        #
        # Measured consequence before the fix: 22-28 Aug reported Rs 4,44,550 and 22-29 Aug, a strict
        # superset, reported Rs 3,34,300. Rs 1,26,328 of real spend read as zero, and a bid rule
        # previewed on the derived window found 743 changes with 0 SB rows where the stored window
        # found 1,005 with 296 — on the one feature in this app that spends money.
        try:
            sb_rows = await reports.fetch_targeting(
                window_start, window_end, ad_product="sb", daily=True, sleep=sleep,
            )
            async with db_factory() as db:
                sb_stored = await repository.save_daily(db, sb_rows, ad_product="sb")
            STATE["sb_rows"] = sb_stored
            logger.info("ads refresh: %d Sponsored Brands daily row(s) stored", sb_stored)
        except AdsError as exc:
            # Isolated: the SP rows above are already committed and current. A throttled SB report is
            # "not now", not a failed refresh — `sbTargeting` has been measured returning 429 after
            # 15 minutes of complete idleness because reports were created earlier that day.
            STATE["sb_error"] = f"The Sponsored Brands report failed: {exc}"
            logger.warning("ads refresh: SB report failed: %s", exc)

        _progress("store", 2, 2)

        STATE.update({"phase": "done", "percent": 100})
        logger.info(
            "ads refresh: %d campaign(s), %d ad group(s), %d window row(s), %d daily row(s) "
            "for %s..%s (purged %d old daily row(s))",
            len(campaigns), len(ad_groups), stored, daily_stored,
            window_start, window_end, purged,
        )

    except AdsNotConfigured as exc:
        STATE.update({"phase": "failed", "error": str(exc)})
        logger.info("ads refresh: skipped, advertising is not configured")
    except AdsError as exc:
        # Amazon's own message is surfaced verbatim: theirs name the cause, and they are how the
        # 31-day report cap and the bid floor were both found.
        STATE.update({"phase": "failed", "error": str(exc)})
        logger.warning("ads refresh failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - the screen must say something rather than hang
        STATE.update({"phase": "failed", "error": f"Unexpected error: {exc}"})
        logger.exception("ads refresh crashed")
    finally:
        # **Load-bearing.** `except Exception` does not catch `asyncio.CancelledError` (a
        # BaseException), and this runs as a fire-and-forget task — exactly the thing cancelled at
        # shutdown. Without `finally` the flag stays True for the life of the process and every
        # later refresh is silently refused. A mutation found this in the portfolio refresh; the
        # three exception tests there all passed with `finally` deleted.
        STATE["running"] = False
        STATE["finished_at"] = datetime.utcnow()

    return status()


def start(**kwargs) -> bool:
    """Fire the refresh as a background task. True if it started, False if one was already running.

    `create_task` rather than `await`: the caller is an HTTP request and the work takes minutes.
    """
    if STATE.get("running"):
        return False
    asyncio.create_task(run(**kwargs))
    return True


async def scheduled_ads_refresh() -> None:
    """The nightly job. Refreshes the default 7-day window so the tab opens on current figures.

    7 days rather than 30 because that is the window a bid decision is actually taken on, and it is
    a single report — attribution-exact and ~6 minutes rather than three chained reports.
    """
    logger.info("scheduled ads refresh starting")
    await run(days=7)
