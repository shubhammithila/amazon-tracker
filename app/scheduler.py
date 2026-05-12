import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import Product, Keyword
from app.scraper.engine import run_scrape, scrape_state
from app.scraper.keyword_tracker import track_keyword_rankings
from app.models import KeywordRanking
from datetime import datetime

logger = logging.getLogger(__name__)
settings = get_settings()

scheduler = AsyncIOScheduler()


async def scheduled_product_scrape():
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

    async def on_complete(results):
        from app.routers.scrape import save_results_to_db
        async with async_session() as db:
            await save_results_to_db(results, db)
        logger.info(f"Scheduled scrape complete: {len(results)} results")

    await run_scrape(asins, on_complete=on_complete)


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


def setup_scheduler():
    if not settings.scheduler_enabled:
        return

    scheduler.add_job(
        scheduled_product_scrape,
        CronTrigger(hour=settings.daily_scrape_hour, minute=settings.daily_scrape_minute),
        id="daily_product_scrape",
        replace_existing=True,
    )

    scheduler.add_job(
        scheduled_keyword_track,
        CronTrigger(hour=settings.daily_scrape_hour + 1, minute=30),
        id="daily_keyword_track",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started: products at {settings.daily_scrape_hour:02d}:{settings.daily_scrape_minute:02d}, "
        f"keywords at {settings.daily_scrape_hour + 1:02d}:30"
    )
