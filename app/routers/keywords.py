import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_

from app.database import get_db
from app.models import Product, Keyword, KeywordRanking
from app.routers.auth import require_auth
from app.scraper.keyword_tracker import track_keyword_rankings

router = APIRouter(prefix="/keywords")


@router.get("")
async def list_keywords(
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    keywords = (await db.execute(
        select(Keyword).where(Keyword.is_active == True).order_by(Keyword.keyword)
    )).scalars().all()

    return JSONResponse([
        {"id": k.id, "keyword": k.keyword, "created_at": k.created_at.isoformat()}
        for k in keywords
    ])


@router.post("")
async def add_keyword(
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    keyword_text = body.get("keyword", "").strip()
    if not keyword_text:
        return JSONResponse({"error": "Keyword is required"}, status_code=400)

    existing = (await db.execute(
        select(Keyword).where(Keyword.keyword == keyword_text)
    )).scalar_one_or_none()

    if existing:
        existing.is_active = True
        await db.commit()
        return JSONResponse({"id": existing.id, "keyword": existing.keyword})

    kw = Keyword(keyword=keyword_text)
    db.add(kw)
    await db.commit()
    await db.refresh(kw)
    return JSONResponse({"id": kw.id, "keyword": kw.keyword})


@router.delete("/{keyword_id}")
async def remove_keyword(
    keyword_id: int,
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    kw = (await db.execute(
        select(Keyword).where(Keyword.id == keyword_id)
    )).scalar_one_or_none()
    if not kw:
        return JSONResponse({"error": "Not found"}, status_code=404)
    kw.is_active = False
    await db.commit()
    return JSONResponse({"message": "Keyword removed"})


@router.post("/track")
async def track_keywords_now(
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    keywords = (await db.execute(
        select(Keyword).where(Keyword.is_active == True)
    )).scalars().all()

    if not keywords:
        return JSONResponse({"error": "No keywords configured"}, status_code=400)

    products = (await db.execute(
        select(Product).where(Product.is_active == True)
    )).scalars().all()
    target_asins = {p.asin for p in products}
    asin_to_id = {p.asin: p.id for p in products}

    if not target_asins:
        return JSONResponse({"error": "No products to track"}, status_code=400)

    now = datetime.utcnow()
    total_found = 0

    for kw in keywords:
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
                total_found += 1

    await db.commit()
    return JSONResponse({"message": f"Tracked {len(keywords)} keywords, found {total_found} rankings"})


@router.get("/{keyword_id}/rankings")
async def get_keyword_rankings(
    keyword_id: int,
    request: Request,
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=30, le=90),
):
    kw = (await db.execute(
        select(Keyword).where(Keyword.id == keyword_id)
    )).scalar_one_or_none()
    if not kw:
        return JSONResponse({"error": "Not found"}, status_code=404)

    since = datetime.utcnow() - timedelta(days=days)
    rankings = (await db.execute(
        select(KeywordRanking)
        .where(and_(KeywordRanking.keyword_id == keyword_id, KeywordRanking.scraped_at >= since))
        .order_by(KeywordRanking.scraped_at)
    )).scalars().all()

    products_map = {}
    for r in rankings:
        product = (await db.execute(
            select(Product).where(Product.id == r.product_id)
        )).scalar_one_or_none()
        if product:
            products_map[product.id] = product.asin

    return JSONResponse({
        "keyword": kw.keyword,
        "rankings": [
            {
                "asin": products_map.get(r.product_id, ""),
                "position": r.rank_position,
                "page": r.page_number,
                "sponsored": r.is_sponsored,
                "date": r.scraped_at.isoformat(),
            }
            for r in rankings
        ],
    })
