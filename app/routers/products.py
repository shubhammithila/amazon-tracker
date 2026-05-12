import io
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, and_

from app.database import get_db
from app.models import Product, PriceHistory, BSRHistory, RatingHistory, SellerOffer
from app.routers.auth import require_auth

router = APIRouter(prefix="/products")


@router.get("")
async def list_products(
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    active_only: bool = True,
):
    query = select(Product)
    if active_only:
        query = query.where(Product.is_active == True)
    query = query.order_by(desc(Product.last_scraped))
    result = await db.execute(query)
    products = result.scalars().all()

    data = []
    for p in products:
        data.append({
            "id": p.id,
            "asin": p.asin,
            "title": p.title,
            "category": p.category,
            "first_seen": p.first_seen.isoformat() if p.first_seen else None,
            "last_scraped": p.last_scraped.isoformat() if p.last_scraped else None,
        })
    return JSONResponse(data)


@router.get("/{asin}/history")
async def product_history(
    asin: str,
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=30, le=90),
):
    product = (await db.execute(
        select(Product).where(Product.asin == asin.upper())
    )).scalar_one_or_none()

    if not product:
        return JSONResponse({"error": "Product not found"}, status_code=404)

    since = datetime.utcnow() - timedelta(days=days)

    prices = (await db.execute(
        select(PriceHistory)
        .where(and_(PriceHistory.product_id == product.id, PriceHistory.scraped_at >= since))
        .order_by(PriceHistory.scraped_at)
    )).scalars().all()

    bsr_entries = (await db.execute(
        select(BSRHistory)
        .where(and_(BSRHistory.product_id == product.id, BSRHistory.scraped_at >= since))
        .order_by(BSRHistory.scraped_at)
    )).scalars().all()

    ratings = (await db.execute(
        select(RatingHistory)
        .where(and_(RatingHistory.product_id == product.id, RatingHistory.scraped_at >= since))
        .order_by(RatingHistory.scraped_at)
    )).scalars().all()

    return JSONResponse({
        "asin": product.asin,
        "title": product.title,
        "prices": [
            {"date": p.scraped_at.isoformat(), "price": float(p.price) if p.price else None,
             "seller": p.seller, "fulfillment": p.fulfillment, "is_deal": p.is_deal}
            for p in prices
        ],
        "bsr": [
            {"date": b.scraped_at.isoformat(), "rank": b.bsr_rank, "category": b.bsr_category}
            for b in bsr_entries
        ],
        "ratings": [
            {"date": r.scraped_at.isoformat(), "rating": float(r.rating) if r.rating else None,
             "count": r.rating_count}
            for r in ratings
        ],
    })


@router.get("/{asin}/sellers")
async def product_sellers(
    asin: str,
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=7, le=30),
):
    product = (await db.execute(
        select(Product).where(Product.asin == asin.upper())
    )).scalar_one_or_none()

    if not product:
        return JSONResponse({"error": "Product not found"}, status_code=404)

    since = datetime.utcnow() - timedelta(days=days)
    offers = (await db.execute(
        select(SellerOffer)
        .where(and_(SellerOffer.product_id == product.id, SellerOffer.scraped_at >= since))
        .order_by(desc(SellerOffer.scraped_at))
    )).scalars().all()

    return JSONResponse({
        "asin": product.asin,
        "offers": [
            {
                "seller": o.seller_name,
                "price": float(o.price) if o.price else None,
                "fulfillment": o.fulfillment,
                "is_buybox": o.is_buybox,
                "condition": o.condition,
                "date": o.scraped_at.isoformat(),
            }
            for o in offers
        ],
    })


@router.get("/download")
async def download_excel(
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    import pandas as pd

    products = (await db.execute(
        select(Product).where(Product.is_active == True).order_by(Product.asin)
    )).scalars().all()

    rows = []
    for p in products:
        latest_price = (await db.execute(
            select(PriceHistory)
            .where(PriceHistory.product_id == p.id)
            .order_by(desc(PriceHistory.scraped_at))
            .limit(1)
        )).scalar_one_or_none()

        latest_bsr = (await db.execute(
            select(BSRHistory)
            .where(BSRHistory.product_id == p.id)
            .order_by(desc(BSRHistory.scraped_at))
            .limit(1)
        )).scalar_one_or_none()

        latest_rating = (await db.execute(
            select(RatingHistory)
            .where(RatingHistory.product_id == p.id)
            .order_by(desc(RatingHistory.scraped_at))
            .limit(1)
        )).scalar_one_or_none()

        rows.append({
            "ASIN": p.asin,
            "Title": p.title or "",
            "Price": f"₹{latest_price.price}" if latest_price and latest_price.price else "",
            "Seller": latest_price.seller if latest_price else "",
            "Fulfillment": latest_price.fulfillment if latest_price else "",
            "Deal": "Yes" if latest_price and latest_price.is_deal else "No",
            "BSR": f"#{latest_bsr.bsr_rank} in {latest_bsr.bsr_category}" if latest_bsr else "",
            "Rating": str(latest_rating.rating) if latest_rating and latest_rating.rating else "",
            "Ratings Count": str(latest_rating.rating_count) if latest_rating and latest_rating.rating_count else "",
            "Last Scraped": p.last_scraped.strftime("%Y-%m-%d %H:%M") if p.last_scraped else "",
        })

    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Products")
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=amazon_tracker.xlsx"},
    )
