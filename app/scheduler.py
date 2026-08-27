import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, delete

from app.config import get_settings
from app.database import async_session
from app.models import (
    Product, Keyword, KeywordRanking,
    PriceHistory, BSRHistory, RatingHistory, SellerOffer,
)
from app.orders import spapi_orders
from app.scraper.engine import run_scrape, scrape_state, ScrapeAlreadyRunning
from app.scraper.keyword_tracker import track_keyword_rankings
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
settings = get_settings()

scheduler = AsyncIOScheduler()

#: How often to pull Amazon orders. Asked for: "every 30 mins is fine + manual refresh
#: button". At 3 pages a run that is 3.8% of the getOrders rate budget, leaving ample room
#: for the manual button and for the 90-day backfill.
ORDER_REFRESH_MINUTES = 30

#: The routine refresh looks back to MIDNIGHT IST, not a number of days.
#:
#: **This has now been wrong in both directions, and the measurements are why.** It was 14 days
#: while the fetch was bounded by order status, which buried today's work under pages of
#: `Delivered`. It then became 90 days once `EasyShipShipmentStatuses` bounded the answer — safe
#: at the time, because `PendingSchedule,PendingPickUp` has almost no history.
#:
#: Adding `PickedUp` to that filter (without which 99 of the day's 194 orders were never
#: fetched at all) reversed the trade-off again: `PickedUp` has MONTHS of history, and
#: `getOrders` pages oldest-first. Measured on 2026-08-26 with the 90-day window: 800 rows
#: across 8 pages, about three minutes, and **0** of them due today.
#:
#: Midnight IST puts today's orders on page 1 — 193 of them, matching Seller Central — because
#: an order dispatched today was necessarily updated today. The deeper catch-up is the manual
#: button's job.
ORDER_REFRESH_DAYS = spapi_orders.TODAY_ONLY

#: Pages per pass. **The old value of 4 was derived from the wrong quantity and truncated every
#: run**, which put a warning on the dispatch screen saying orders were missing from today's
#: sheet.
#:
#: The reasoning it replaced was "a day's dispatch is ~200 orders = 2 pages, so 4 is double".
#: That measures the wrong set. `LastUpdatedAfter=midnight IST` does not mean "orders FOR today";
#: it means every order Amazon TOUCHED today — and Amazon touches an order on every status
#: move, so yesterday's 188 and Sunday's 264 are all in the answer as they walk
#: `PendingPickUp → PickedUp → OutForDelivery → Delivered`. Measured on 2026-08-27: 1,790 rows
#: match the status filter and ~600 are updated on a busy day.
#:
#: 12 pages = 1,200 orders, so the pass reaches its natural end and the warning stops firing.
#: The cost is affordable because it is a CEILING, not the usual spend: a typical run stops at
#: ~6 pages (~2.2 min) when Amazon returns no NextToken. Worst case is 12 × 22.5s = 4.5 minutes,
#: comfortably inside the 30-minute interval, and 48 runs × 12 pages is 576 of the 3,840 daily
#: getOrders calls — 15% of the budget.
#:
#: Reaching the cap is still reported rather than silently truncating; see
#: `spapi_orders.fetch_easy_ship_orders` for why that message must not claim today's sheet is
#: incomplete.
ORDER_REFRESH_PAGES = 12


async def scheduled_product_scrape():
    # Cheap pre-check so we don't load every ASIN for nothing. run_scrape()
    # performs the authoritative atomic claim and raises ScrapeAlreadyRunning
    # if a manual run wins the race, so this is an optimisation, not the guard.
    if scrape_state.running:
        logger.info("Scrape already running, skipping scheduled run")
        return

    async with async_session() as db:
        products = (await db.execute(
            select(Product).where(Product.is_active == True)
        )).scalars().all()
        asins = [p.asin for p in products]

    if not asins:
        logger.info("No active products to scrape")
        return

    logger.info(f"Starting scheduled scrape of {len(asins)} products")

    # Save each result immediately as it comes in (don't buffer all 250+ in memory)
    async def on_result(result):
        if result.get("status") == "OK":
            try:
                from app.routers.scrape import save_results_to_db
                async with async_session() as db:
                    await save_results_to_db([result], db)
            except Exception as e:
                logger.warning(f"Failed to save result for {result.get('asin')}: {e}")

    async def on_complete(results):
        logger.info(f"Scheduled scrape complete: {len(results)} results")

    try:
        await run_scrape(asins, on_result=on_result, on_complete=on_complete)
    except ScrapeAlreadyRunning:
        # A manual scrape claimed the scraper between the pre-check and here.
        # Never touch scrape_state — those results belong to the other run.
        logger.info("Scheduled scrape skipped: a manual scrape started first")
        return

    # Free the buffered results now that every row is persisted. Safe here
    # because the atomic claim guarantees this run owned the state; the previous
    # unconditional clears could wipe a concurrent manual scrape's results.
    scrape_state.results.clear()


async def scheduled_keyword_track():
    async with async_session() as db:
        keywords = (await db.execute(
            select(Keyword).where(Keyword.is_active == True)
        )).scalars().all()

        products = (await db.execute(
            select(Product).where(Product.is_active == True)
        )).scalars().all()

        if not keywords or not products:
            return

        target_asins = {p.asin for p in products}
        asin_to_id = {p.asin: p.id for p in products}
        now = datetime.utcnow()

        for kw in keywords:
            try:
                rankings = await track_keyword_rankings(kw.keyword, target_asins)
                for r in rankings:
                    product_id = asin_to_id.get(r["asin"])
                    if product_id:
                        db.add(KeywordRanking(
                            keyword_id=kw.id,
                            product_id=product_id,
                            rank_position=r["rank_position"],
                            page_number=r["page_number"],
                            is_sponsored=r["is_sponsored"],
                            scraped_at=now,
                        ))
            except Exception as e:
                logger.error(f"Keyword tracking failed for '{kw.keyword}': {e}")

        await db.commit()
    logger.info("Scheduled keyword tracking complete")


async def scheduled_purge_old_history():
    """Delete history rows older than settings.data_retention_days.

    The setting existed but nothing ever read it, so price/BSR/rating/seller
    history grew without bound. At ~250 ASINs scraped daily that is ~91k rows a
    year per table on a t2.micro with a 90-day retention policy on paper.
    """
    days = settings.data_retention_days
    if not days or days <= 0:
        logger.info("Data retention disabled (data_retention_days <= 0)")
        return

    cutoff = datetime.utcnow() - timedelta(days=days)
    deleted_total = 0

    async with async_session() as db:
        for model in (PriceHistory, BSRHistory, RatingHistory, SellerOffer, KeywordRanking):
            result = await db.execute(
                delete(model).where(model.scraped_at < cutoff)
            )
            count = result.rowcount or 0
            deleted_total += count
            if count:
                logger.info(f"Retention: deleted {count} rows from {model.__tablename__}")
        await db.commit()

    # Amazon orders are purged separately, because they have no `scraped_at` — the age of
    # an order is its PURCHASE date. Routed through the repository rather than a DELETE
    # here, so the cascade to amazon_order_items goes through the ORM and this file does
    # not become a second place that knows how order rows are shaped.
    from app.orders import repository as orders_repo

    async with async_session() as db:
        orders_gone = await orders_repo.purge_older_than(db, days)
    if orders_gone:
        deleted_total += orders_gone
        logger.info(f"Retention: deleted {orders_gone} rows from amazon_orders")

    logger.info(
        f"Retention sweep complete: {deleted_total} rows older than {days} days removed"
    )


async def scheduled_order_refresh():
    """Pull Amazon Easy Ship orders into the local tables, every 30 minutes.

    **30 minutes is the interval the owner asked for, and it costs almost nothing.**
    `getOrders` allows one call every 22.5 seconds — 3,840 a day — and this uses about
    3.8% of that: 48 runs of up to 3 pages. The manual Refresh button on the Orders tab
    spends one more call when someone wants it sooner.

    **It pages, and that is not optional.** Amazon caps a page at 100 orders and the
    measured actionable backlog is 371, so a single-page refresh would miss most of it.
    `ORDER_REFRESH_PAGES` must stay comfortably above the number of orders that can need
    work at once.

    **The window is wide because the fetch is bounded by STATUS, not by date.** This used to
    look back 14 days to keep the cost down, and that was precisely the bug: `getOrders`
    pages oldest-first, so a date-bounded window filled with `Delivered` orders and the ones
    needing work fell past the page cap. Filtering on `EasyShipShipmentStatuses` bounds the
    result by relevance, which makes a 90-day window cost the same 4 pages as a 14-day one
    while also catching an order that has been unshipped for three weeks.

    Skipped silently when a refresh is already running — `refresh.run` refuses and returns
    rather than raising, so an overlap with a long manual backfill is a no-op instead of an
    error in the log every half hour.
    """
    from app.orders import refresh

    # Read the settings FRESH rather than using this module's import-time `settings`.
    # That snapshot is bound once when the module loads, so a credential added to .env
    # after start — or cleared, as a test does — would never be seen. A job that runs
    # every 30 minutes for the life of the process should not be deciding on a value
    # captured at boot.
    if not get_settings().spapi_configured:
        # Not an error: the app is expected to work without Amazon credentials, and the
        # Orders tab says so on screen rather than the log filling with failures every
        # half hour on every fresh install.
        logger.debug("Order refresh skipped: SP-API is not configured")
        return

    result = await refresh.run(days=ORDER_REFRESH_DAYS, max_pages=ORDER_REFRESH_PAGES)
    if result.get("refused"):
        logger.info("Order refresh skipped: one is already running")
    elif result.get("error"):
        logger.warning("Order refresh failed: %s", result["error"])
    else:
        logger.info(
            "Order refresh: %d seen, %d new, %d updated, %d reconciled, %d itemised",
            result["orders_seen"], result["created"], result["updated"],
            result.get("reconciled", 0), result["items_fetched"],
        )


def setup_scheduler():
    """Register the background jobs. Nothing runs unless a flag asks for it.

    **The guard is per-job rather than at the top, and that is deliberate.** This function used
    to return early on `not settings.scheduler_enabled`, which meant the only way to get the
    half-hourly orders refresh was to also wake the product scrape, the keyword track and the
    retention purge. Production runs with the master flag off precisely to keep those asleep on
    a 951 MB box with no swap, so the orders refresh needed its own switch.
    """
    if not (settings.scheduler_enabled or settings.order_refresh_enabled):
        return

    parts = []
    if settings.scheduler_enabled:
        scheduler.add_job(
            scheduled_product_scrape,
            CronTrigger(hour=settings.daily_scrape_hour, minute=settings.daily_scrape_minute),
            id="daily_product_scrape",
            replace_existing=True,
        )

        # Wrap with % 24 — a daily_scrape_hour of 23 would otherwise build an
        # invalid CronTrigger(hour=24) and crash scheduler setup at startup.
        keyword_hour = (settings.daily_scrape_hour + 1) % 24
        scheduler.add_job(
            scheduled_keyword_track,
            CronTrigger(hour=keyword_hour, minute=30),
            id="daily_keyword_track",
            replace_existing=True,
        )

        # Purge after both scrapes so a run is never competing with deletes.
        purge_hour = (settings.daily_scrape_hour + 3) % 24
        scheduler.add_job(
            scheduled_purge_old_history,
            CronTrigger(hour=purge_hour, minute=15),
            id="daily_history_purge",
            replace_existing=True,
        )
        parts += [
            f"products at {settings.daily_scrape_hour:02d}:{settings.daily_scrape_minute:02d}",
            f"keywords at {keyword_hour:02d}:30",
            f"history purge at {purge_hour:02d}:15 (retention {settings.data_retention_days}d)",
        ]

    # Every 30 minutes, and jittered by starting 4 minutes in rather than on the hour, so
    # an order refresh never begins in the same second as the 06:00 product scrape on a
    # 951 MB box.
    #
    # Registered on EITHER flag: `order_refresh_enabled` lets production run this alone, while
    # `scheduler_enabled` keeps it working for any installation that never learns about the
    # second flag.
    scheduler.add_job(
        scheduled_order_refresh,
        IntervalTrigger(minutes=ORDER_REFRESH_MINUTES, start_date=None),
        id="order_refresh",
        replace_existing=True,
        # A slow run must not stack up behind itself. refresh.run refuses a concurrent
        # start anyway, but coalescing keeps APScheduler from queueing missed runs.
        max_instances=1,
        coalesce=True,
    )
    parts.append(f"orders every {ORDER_REFRESH_MINUTES}m")

    scheduler.start()
    # Built from `parts` rather than one f-string: `keyword_hour` and `purge_hour` only exist
    # when the master flag is on, so interpolating them unconditionally would raise NameError
    # on the orders-only path — a crash at startup on exactly the configuration production uses.
    logger.info("Scheduler started: %s", ", ".join(parts))
