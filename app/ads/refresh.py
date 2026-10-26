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

from app import ist
from app.ads import listings, repository, reports, spapi_ads
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
        #: The product-ad report (SKU rules) and the product-ad list, failing on their own like SB:
        #: the keyword figures above are already stored when this runs.
        "product_rows": 0,
        "product_ads": 0,
        "product_error": None,
        #: The search-term reports (negative rules) and the negatives list, failing on their own too.
        "search_rows": 0,
        "negatives": 0,
        "search_error": None,
        "window_start": None,
        "window_end": None,
        #: {"sp": [start, end] | None, "sb": ...}: what this run asked Amazon for.
        "fetched": None,
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

    **"Yesterday" is the IST yesterday, not the server's.** `date.today()` on this box is the UTC
    date, so between 00:00 and 05:30 IST it is a day behind — and this function decides both what the
    nightly job fetches AND what the screen's "last 7 days" means, so a wrong day here puts the
    coverage and the presets one day apart for 5.5 hours out of every 24. The sixth instance of that
    defect class in this codebase; `app/ist.py` records the other five.
    """
    end = (today or ist.today()) - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


#: The two report types a refresh can request. Always both: the account runs Sponsored Brands, and a
#: product left out of the plan would never fill its gaps.
AD_PRODUCTS = ("sp", "sb")

#: **The nightly job re-fetches only the last 14 days, not all 60.** Amazon credits a sale to the
#: click's day for up to 14 days afterwards (`attributedSalesSameSku14d`), so a day older than that
#: no longer changes and fetching it again buys nothing. Re-fetching 60 days was two reports per ad
#: type a night, and Amazon's Sponsored Brands allowance is a few reports a DAY: measured on
#: production, the second SB report was refused on alternate nights (runs 30, 32, 34, 36, 39 and 40),
#: which is what left the tab reading "Sponsored Brands is missing 2 of these days" most mornings.
#: Fourteen days is one report per ad type.
NIGHTLY_SETTLE_DAYS = 14

#: When the owner presses Refresh on a window that is already complete, the last few days are
#: re-fetched so the press still brings fresh figures — for **Sponsored Products only**. SB is the
#: report Amazon rations, and spending one on days we already hold is how a manual press used up the
#: allowance the nightly job needed.
MANUAL_FRESHEN_DAYS = 3


def plan_ranges(
    window_start: str,
    window_end: str,
    held: dict[str, set[str]],
    *,
    settle_days: int = 0,
    freshen_days: int = 0,
) -> dict[str, tuple[str, str] | None]:
    """Per ad product, the one contiguous range to fetch — or ``None`` to skip it. Pure.

    The range runs from the EARLIER of (the first day missing in the window, the start of the
    settling tail) to the window's end. One contiguous span rather than a list of gaps, because each
    span is a report and a report's cost is mostly Amazon's queue time, not its length: three
    one-day reports cost three times one three-day report.

    ``freshen_days`` applies only when NOTHING is missing for EITHER product, and only to Sponsored
    Products — the manual-press fallback described at `MANUAL_FRESHEN_DAYS`.
    """
    first_day, last_day = date.fromisoformat(window_start), date.fromisoformat(window_end)
    wanted = repository.expected_days(window_start, window_end)

    def settle_start(n: int) -> str | None:
        if n <= 0:
            return None
        return max(first_day, last_day - timedelta(days=n - 1)).isoformat()

    plan: dict[str, tuple[str, str] | None] = {}
    for product in AD_PRODUCTS:
        have = held.get(product, set())
        missing = [d for d in wanted if d not in have]
        starts = [s for s in (missing[0] if missing else None, settle_start(settle_days)) if s]
        plan[product] = (min(starts), window_end) if starts else None

    if freshen_days and not any(plan.values()):
        plan["sp"] = (settle_start(freshen_days), window_end)
    return plan


def plan_product_range(window_start: str, window_end: str, held: set[str], *,
                       settle_days: int = 0, freshen_days: int = 0) -> tuple[str, str] | None:
    """The one range of the PRODUCT-AD report to fetch, by `plan_ranges`' own rule for Sponsored
    Products: from the first missing day (or the settling tail) to the window's end, a short
    freshen when nothing is missing and the owner pressed Refresh, else nothing. Pure.

    Delegates rather than restating the rule, with the other product marked fully held so its
    absence cannot suppress the freshen.
    """
    everything = set(repository.expected_days(window_start, window_end))
    return plan_ranges(window_start, window_end, {"sp": held, "sb": everything},
                       settle_days=settle_days, freshen_days=freshen_days)["sp"]


async def _held_days(db_factory) -> dict[str, set[str]]:
    """Days held per ad product, plus `"product"`: the days of the product-ad (SKU) table."""
    async with db_factory() as db:
        held = {p: await repository.daily_days_held(db, ad_product=p) for p in AD_PRODUCTS}
        held["product"] = await repository.product_days_held(db)
        from app.ads import search_store
        terms = await search_store.search_days_held(db)
        held["search_sp"] = terms.get("sp", set())
        held["search_sb"] = terms.get("sb", set())
        return held


async def _attribute_sb_to_portfolio(db_factory, start: str, end: str) -> None:
    """Copy the SB spend just stored onto the Portfolio tab's rows, attributed per ASIN.

    **This is the nightly path for Portfolio's Sponsored Brands figures**, and it lives here rather
    than in the Portfolio refresh on purpose: that refresh creating its own `sbTargeting` report
    would spend the hours-long SB throttle budget this job needs 30 minutes later. See
    `app/portfolio/sb_attribution.py`.

    **Swallowed and logged, never raised.** It runs inside `store_sb_chunk`, whose exceptions
    propagate out of `fetch_targeting` — so a bug or an Amazon hiccup here would otherwise fail the
    Ads tab's own SB report, which is the report this app spent a week getting to succeed. The
    Portfolio rows keep their last good figures when it fails.
    """
    from datetime import date, timedelta

    from app.portfolio import sb_attribution

    first, last = date.fromisoformat(start), date.fromisoformat(end)
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    try:
        async with db_factory() as db:
            await sb_attribution.attribute_days(db, days)
    except Exception:  # noqa: BLE001 - must never fail the Ads tab's own refresh
        logger.warning("ads refresh: attributing SB spend to the Portfolio failed", exc_info=True)


async def run(
    *,
    days: int = 7,
    start: str | None = None,
    end: str | None = None,
    sleep=None,
    db_factory=async_session,
    today: date | None = None,
    settle_days: int = 0,
    freshen_days: int = 0,
    only_if_missing: bool = False,
) -> dict:
    """Refresh the entity cache and one window of performance. Returns the final `status()`.

    **Fetches only what the window is missing, per ad product** (`plan_ranges`), plus the last
    ``settle_days`` days whose attributed sales may still move. Reported as *"the refresh is taking
    too much time"*: the button re-downloaded Sponsored Products for the whole window (~7 minutes)
    when SP already held every day and only two days of Sponsored Brands were missing.

    ``only_if_missing`` is the afternoon retry's mode: with nothing missing it returns at once,
    without a single Amazon call, so running it daily costs nothing on a good day.

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

    # **Plan only over days the retention purge KEEPS.** `default_window(60)` starts one day before
    # the 60 days `purge_daily` holds, so that first day is always "missing" — found by dry-running
    # this plan against production, where the nightly and gap-fill plans both came out as the full
    # 60 days again, purely to re-fetch a day the purge deletes minutes later. The IST day is used,
    # and is never earlier than the purge's server-clock day, so no planned day can be one it purges.
    keep_from = ((today or ist.today())
                 - timedelta(days=repository.DAILY_RETENTION_DAYS - 1)).isoformat()
    held = await _held_days(db_factory)
    plan = plan_ranges(
        max(window_start, keep_from), window_end, held,
        settle_days=settle_days, freshen_days=freshen_days,
    )
    # The product report freshens on a manual press only when the keyword reports are freshening too
    # (nothing missing for either), the same rule SP follows: a press that is already fetching SB
    # brings fresh figures without a third report.
    keywords_complete = not any(plan_ranges(
        max(window_start, keep_from), window_end, held, settle_days=settle_days).values())
    product_plan = plan_product_range(
        max(window_start, keep_from), window_end, held.get("product") or set(),
        settle_days=settle_days, freshen_days=freshen_days if keywords_complete else 0,
    )
    # The search-term reports (negative rules): SP planned like the product report; SB likewise, but
    # only ASKED for after this run's SB keyword report landed, because SB reports are rationed and
    # the keyword one is the one the bid rules need.
    search_plans = {p: plan_product_range(
        max(window_start, keep_from), window_end, held.get(f"search_{p}") or set(),
        settle_days=settle_days, freshen_days=freshen_days if keywords_complete else 0,
    ) for p in ("sp", "sb")}
    if only_if_missing and not any(plan.values()) and not product_plan and not any(search_plans.values()):
        logger.info("ads refresh: %s..%s is complete for every ad product, nothing to fetch",
                    window_start, window_end)
        return {**status(), "skipped": True}

    reset_state()
    STATE.update({
        "running": True,
        "started_at": datetime.utcnow(),
        "phase": "campaigns",
        "window_start": window_start,
        "window_end": window_end,
        # What this run will actually ask Amazon for, so the screen can say "fetched SB 3-4 Oct"
        # rather than implying the whole window was re-downloaded.
        "fetched": {**{p: (list(r) if r else None) for p, r in plan.items()},
                    "product": list(product_plan) if product_plan else None,
                    **{f"search_{p}": (list(r) if r else None) for p, r in search_plans.items()}},
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

            # ── Product ads: the SKU list and each one's state ──
            #
            # What the last-active guard reads at preview: an ad group must keep one enabled SKU.
            # ~13 pages, seconds. Isolated, because the keyword figures do not depend on it.
            try:
                product_ads = await spapi_ads.fetch_product_ads(client)
                async with db_factory() as db:
                    STATE["product_ads"] = await repository.replace_product_ads(db, product_ads)
            except AdsError as exc:
                STATE["product_error"] = f"The product-ad list could not be read: {exc}"
                logger.warning("ads refresh: product ads failed: %s", exc)


            # ── Listings: every SKU on the account, for the SKU coverage check ──
            # SP-API, not the Advertising API; ~30 s; never raises and records its own failure.
            await listings.refresh_if_stale(db_factory, client, sleep=sleep or asyncio.sleep)
            _progress("ad_groups", 2, 2)

        # ── The report ──
        #
        # Outside the client above: `fetch_targeting` opens its own, because a 90-day window holds
        # the connection for three consecutive reports and a single long-lived client across all of
        # it is more likely to be dropped mid-poll.
        def on_report_progress(done, total):
            _progress("report", done, total)

        # ── The report, stored CHUNK BY CHUNK ──
        #
        # **DAILY, not SUMMARY.** Per-day rows are what make an arbitrary sub-range instant: with
        # them, "I have 60 days, show me 20" is a GROUP BY rather than another ~20-minute report.
        # Measured: DAILY returns 3.6x the rows (45,650 vs 12,854 for 7 days) and bulk-inserts at
        # 30,921 rows/sec.
        #
        # **Each chunk commits as it lands**, because Amazon caps one report at 31 days and the
        # nightly window is 60 — two chunks per ad product, four reports a night, any of which can be
        # throttled for hours. Accumulating them all before the first write meant one 429 discarded
        # up to 40 minutes of successfully fetched data. A partial night now leaves real days stored,
        # and `repository.daily_range_complete` makes any range touching the gap decline to answer
        # rather than sum short.
        #
        # ONE grain: the per-day rows, which every window is summed from. There used to be a second,
        # window-grain write here, and keeping both is what let two code paths answer the same
        # question differently — Sponsored Brands went to the window table and not to the daily one,
        # so a window nobody had fetched exactly under-reported by 28% of spend.
        _progress("store", 0, 2)
        STATE["daily_rows"] = 0

        async def store_sp_chunk(chunk_rows, chunk_start, chunk_end):
            async with db_factory() as chunk_db:
                stored = await repository.save_daily(chunk_db, chunk_rows, ad_product="sp")
            STATE["daily_rows"] = STATE.get("daily_rows", 0) + stored
            STATE["rows"] = STATE["daily_rows"]
            logger.info(
                "ads refresh: stored %d daily row(s) for %s..%s", stored, chunk_start, chunk_end
            )

        if plan["sp"]:
            await reports.fetch_targeting(
                *plan["sp"], daily=True,
                sleep=sleep, on_progress=on_report_progress, on_chunk=store_sp_chunk,
            )
        else:
            logger.info("ads refresh: Sponsored Products already holds %s..%s, not re-requested",
                        window_start, window_end)
        daily_stored = STATE["daily_rows"]

        async with db_factory() as db:
            _progress("store", 1, 2)
            # Keep the daily table bounded — production is at 87% disk and every deploy copies the
            # whole database.
            purged = await repository.purge_daily(db)
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
        STATE["sb_rows"] = 0

        async def store_sb_chunk(chunk_rows, chunk_start, chunk_end):
            """Per chunk, like Sponsored Products — and it matters MORE here, because `sbTargeting`
            is the report that throttles for hours."""
            async with db_factory() as chunk_db:
                stored = await repository.save_daily(chunk_db, chunk_rows, ad_product="sb")
            STATE["sb_rows"] = STATE.get("sb_rows", 0) + stored
            logger.info(
                "ads refresh: stored %d Sponsored Brands daily row(s) for %s..%s",
                stored, chunk_start, chunk_end,
            )
            await _attribute_sb_to_portfolio(db_factory, chunk_start, chunk_end)

        try:
            if plan["sb"]:
                await reports.fetch_targeting(
                    *plan["sb"], ad_product="sb", daily=True, sleep=sleep,
                    on_chunk=store_sb_chunk,
                )
                logger.info(
                    "ads refresh: %d Sponsored Brands daily row(s) stored", STATE["sb_rows"]
                )
            else:
                # Not requested at all: SB is the report Amazon rations to a few a day, and asking
                # for days already held is how the nightly job used to lose its own allowance.
                logger.info("ads refresh: Sponsored Brands already holds %s..%s, not re-requested",
                            window_start, window_end)
        except AdsError as exc:
            # Isolated: the SP rows above are already committed and current. A throttled SB report is
            # "not now", not a failed refresh — `sbTargeting` has been measured returning 429 after
            # 15 minutes of complete idleness because reports were created earlier that day.
            STATE["sb_error"] = f"The Sponsored Brands report failed: {exc}"
            logger.warning("ads refresh: SB report failed: %s", exc)

        # ── SP product ads (SKU rules), per day ──
        #
        # A THIRD report, after the keyword ones and isolated like Sponsored Brands: the product rows
        # are the same money as the keyword rows from the other side, so they go to their own table
        # and a failure here costs nothing above.
        STATE["product_rows"] = 0

        async def store_product_chunk(chunk_rows, chunk_start, chunk_end):
            async with db_factory() as chunk_db:
                stored = await repository.save_product_daily(chunk_db, chunk_rows)
            STATE["product_rows"] = STATE.get("product_rows", 0) + stored
            logger.info("ads refresh: stored %d product-ad daily row(s) for %s..%s",
                        stored, chunk_start, chunk_end)

        try:
            if product_plan:
                await reports.fetch_targeting(
                    *product_plan, report="product", sleep=sleep, on_chunk=store_product_chunk,
                )
            else:
                logger.info("ads refresh: product ads already hold %s..%s, not re-requested",
                            window_start, window_end)
        except AdsError as exc:
            STATE["product_error"] = f"The product-ad report failed: {exc}"
            logger.warning("ads refresh: product-ad report failed: %s", exc)

        # ── Search terms (negative rules), per day: SP, then SB only if SB landed this run ──
        STATE["search_rows"] = 0
        for product, search_plan in search_plans.items():
            if not search_plan:
                continue
            if product == "sb" and STATE.get("sb_error"):
                logger.info("ads refresh: SB search terms not requested, the SB keyword report failed")
                continue

            async def store_search_chunk(chunk_rows, chunk_start, chunk_end, _product=product):
                from app.ads import search_store
                async with db_factory() as chunk_db:
                    stored = await search_store.save_search_term_daily(chunk_db, chunk_rows, ad_product=_product)
                STATE["search_rows"] = STATE.get("search_rows", 0) + stored

            try:
                await reports.fetch_targeting(*search_plan, report="search", ad_product=product,
                                              sleep=sleep, on_chunk=store_search_chunk)
            except AdsError as exc:
                name = "Sponsored Brands" if product == "sb" else "Sponsored Products"
                STATE["search_error"] = f"The {name} search-term report failed: {exc}"
                logger.warning("ads refresh: %s search-term report failed: %s", product, exc)

        # ── Existing negatives of the ad groups that HAVE search terms, so a rule never offers a
        # duplicate. Not the whole account: measured, it holds 112,495 negatives (83,773 SP negative
        # products) and listing them took 152 s — too much to hold on a 951 MB box every night.
        try:
            from app.ads import search_store
            async with db_factory() as db:
                groups = await search_store.search_ad_groups(db)
            if groups:
                async with httpx.AsyncClient(timeout=settings.ads_timeout) as neg_client:
                    found = await spapi_ads.fetch_negatives(neg_client, ad_group_ids=groups)
                async with db_factory() as db:
                    STATE["negatives"] = await search_store.replace_negatives(db, found)
        except AdsError as exc:
            STATE["search_error"] = f"The existing negatives could not be read: {exc}"
            logger.warning("ads refresh: negatives failed: %s", exc)

        _progress("store", 2, 2)

        STATE.update({"phase": "done", "percent": 100})
        logger.info(
            "ads refresh: %d campaign(s), %d ad group(s), %d SP daily row(s) for %s, "
            "%d SB daily row(s) for %s, window %s..%s (purged %d old daily row(s))",
            len(campaigns), len(ad_groups), daily_stored, plan["sp"] or "nothing",
            STATE.get("sb_rows", 0), plan["sb"] or "nothing", window_start, window_end, purged,
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

        # **The run record is written HERE, in the `finally`, for the same reason the flag is.**
        # A cancelled or crashed run is the case that most needs explaining afterwards, and it is
        # exactly the case a write on the success path would skip. The whole reason this table exists
        # is that the equivalent state lived in `STATE` and a deploy erased it: on 1 Sep the run stored
        # 482,578 SP rows, Amazon throttled the SB report, the app restarted, and the Ads tab could
        # only say "nothing fetched" while half a million current rows sat in the table.
        #
        # Its own session and its own try: this must not convert a completed refresh into a failed one
        # because the bookkeeping could not be written.
        try:
            async with db_factory() as db:
                await repository.record_refresh(
                    db,
                    window_start=STATE.get("window_start"),
                    window_end=STATE.get("window_end"),
                    sp_rows=STATE.get("daily_rows") or 0,
                    sb_rows=STATE.get("sb_rows") or 0,
                    campaigns=STATE.get("campaigns") or 0,
                    ad_groups=STATE.get("ad_groups") or 0,
                    error=STATE.get("error"),
                    sb_error=STATE.get("sb_error"),
                    started_at=STATE.get("started_at"),
                    product_rows=STATE.get("product_rows") or 0,
                    product_error=STATE.get("product_error"),
                    search_rows=STATE.get("search_rows") or 0,
                    search_error=STATE.get("search_error"),
                )
        except Exception:  # noqa: BLE001 - bookkeeping must never fail the run it describes
            logger.exception("ads refresh: could not record the run")

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
