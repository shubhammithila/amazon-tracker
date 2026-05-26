import asyncio
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models import Product, PriceHistory, BSRHistory, RatingHistory, SellerOffer, ScrapeJob
from app.scraper.engine import run_scrape, scrape_state, ScrapeState
from app.routers.auth import require_auth
from app.config import get_settings

router = APIRouter()
settings = get_settings()

_scrape_task: Optional[asyncio.Task] = None


async def save_results_to_db(results: list[dict], db: AsyncSession):
    now = datetime.utcnow()
    for r in results:
        if r.get("status") != "OK":
            continue

        asin = r["asin"]
        product = (await db.execute(
            select(Product).where(Product.asin == asin)
        )).scalar_one_or_none()

        if not product:
            product = Product(asin=asin, title=r.get("title"), use_by=r.get("use_by"), first_seen=now)
            db.add(product)
            await db.flush()
        else:
            product.title = r.get("title") or product.title
            product.use_by = r.get("use_by") or product.use_by
            product.last_scraped = now

        price_val = None
        if r.get("price"):
            price_str = re.sub(r"[^\d.]", "", r["price"])
            try:
                price_val = float(price_str)
            except ValueError:
                pass

        if price_val is not None:
            db.add(PriceHistory(
                product_id=product.id,
                price=price_val,
                seller=r.get("seller"),
                fulfillment=r.get("fulfillment"),
                is_deal=(r.get("deal") == "Yes"),
                scraped_at=now,
            ))

        if r.get("bsr_numeric"):
            db.add(BSRHistory(
                product_id=product.id,
                bsr_rank=r["bsr_numeric"],
                bsr_category=r.get("bsr_category"),
                scraped_at=now,
            ))

        rating_val = None
        if r.get("rating"):
            try:
                rating_val = float(r["rating"])
            except ValueError:
                pass

        rating_count_val = None
        if r.get("rating_count"):
            try:
                rating_count_val = int(r["rating_count"])
            except ValueError:
                pass

        if rating_val is not None:
            db.add(RatingHistory(
                product_id=product.id,
                rating=rating_val,
                rating_count=rating_count_val,
                scraped_at=now,
            ))

    await db.commit()


async def _run_scrape_task(asins: list[str]):
    import logging
    logger = logging.getLogger(__name__)
    from app.database import async_session

    async def on_complete(results):
        try:
            async with async_session() as db:
                await save_results_to_db(results, db)
        except Exception as e:
            logger.exception(f"Failed to save results to DB: {e}")

    try:
        await run_scrape(asins, on_complete=on_complete)
    except Exception as e:
        logger.exception(f"Scrape task crashed: {e}")
        scrape_state.error = str(e)
        scrape_state.running = False


@router.post("/scrape")
async def start_scrape(request: Request, _=Depends(require_auth)):
    global _scrape_task

    body = await request.json()
    asins_raw = body.get("asins", "")

    if isinstance(asins_raw, str):
        asins = [a.strip() for a in re.split(r"[,\s\n]+", asins_raw) if a.strip()]
    else:
        asins = [a.strip() for a in asins_raw if a.strip()]

    asins = list(dict.fromkeys(a.upper() for a in asins if re.match(r"^B0[A-Z0-9]{8}$", a.upper())))

    if not asins:
        return JSONResponse({"error": "No valid ASINs provided"}, status_code=400)

    if scrape_state.running:
        return JSONResponse({"error": "Scrape already in progress"}, status_code=409)

    _scrape_task = asyncio.create_task(_run_scrape_task(asins))
    return JSONResponse({"message": f"Scraping {len(asins)} ASINs", "total": len(asins)})


@router.get("/progress")
async def get_progress(request: Request, _=Depends(require_auth)):
    return JSONResponse(scrape_state.to_dict())


@router.get("/results")
async def get_results(request: Request, _=Depends(require_auth)):
    return JSONResponse({
        "results": scrape_state.results,
        "last_scraped_at": scrape_state.last_scraped_at,
    })


@router.post("/stop")
async def stop_scrape(request: Request, _=Depends(require_auth)):
    global _scrape_task
    if scrape_state.running:
        scrape_state.stop()
        if _scrape_task:
            _scrape_task.cancel()
            _scrape_task = None
        return JSONResponse({"message": "Scrape stopped"})
    return JSONResponse({"message": "No scrape running"})


@router.post("/fetch-sheet")
async def fetch_sheet(request: Request, _=Depends(require_auth)):
    import httpx

    body = await request.json()
    url = body.get("url", "")

    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        return JSONResponse({"error": "Invalid Google Sheets URL"}, status_code=400)

    sheet_id = match.group(1)
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(csv_url)
        if resp.status_code != 200:
            return JSONResponse({"error": "Failed to fetch sheet"}, status_code=400)

    lines = resp.text.strip().split("\n")
    asins = set()
    for line in lines:
        cols = line.split(",")
        for col in cols:
            col = col.strip().strip('"').strip()
            if re.match(r"^B0[A-Z0-9]{8}$", col.upper()):
                asins.add(col.upper())

    return JSONResponse({"asins": sorted(asins), "count": len(asins)})
