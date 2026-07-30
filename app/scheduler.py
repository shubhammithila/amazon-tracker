import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, delete

from app.config import get_settings
from app.database import async_session
from app.models import (
    Product, Keyword, KeywordRanking,
    PriceHistory, BSRHistory, RatingHistory, SellerOffer,
)
from app.scraper.engine import run_scrape, scrape_state, ScrapeAlreadyRunning
from app.scraper.keyword_tracker import track_keyword_rankings
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
settings = get_settings()

scheduler = AsyncIOScheduler()


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

    logger.info(
        f"Retention sweep complete: {deleted_total} rows older than {days} days removed"
    )


def setup_scheduler():
    if not settings.scheduler_enabled:
        return

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

    scheduler.start()
    logger.info(
        f"Scheduler started: products at {settings.daily_scrape_hour:02d}:{settings.daily_scrape_minute:02d}, "
        f"keywords at {keyword_hour:02d}:30, "
        f"history purge at {purge_hour:02d}:15 (retention {settings.data_retention_days}d)"
    )
