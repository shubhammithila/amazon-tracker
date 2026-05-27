"""Portfolio Review — upload Business Report CSV, aggregate by Parent ASIN, surface underperformers."""
import io
import re
import logging
from datetime import datetime, timedelta

import pandas as pd
from fastapi import APIRouter, Request, Depends, UploadFile, File, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_

from app.database import get_db
from app.models import Product, RatingHistory
from app.routers.auth import require_auth

router = APIRouter(prefix="/churn")
logger = logging.getLogger(__name__)


# ─── CSV Parsing ─────────────────────────────────────────────────────────────

def _clean_number(val) -> float:
    """Strip ₹, commas, % and return float."""
    if val is None or str(val).strip() in ("", "-", "nan"):
        return 0.0
    cleaned = re.sub(r"[₹,%\s]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _detect_brand(title: str) -> str:
    """Extract brand from product title."""
    t = title.upper()
    if "MITHILA FOODS" in t or "MITHILA" in t:
        return "Mithila Foods"
    if "HOWRAH FOODS" in t or "HOWRAH" in t:
        return "Howrah Foods"
    return "Other"


def parse_and_aggregate(content: bytes) -> list[dict]:
    """
    Parse Amazon Business Report CSV and aggregate by Parent ASIN.
    Sums: sessions, units_ordered, revenue, total_order_items
    Averages: conversion_rate, buy_box_pct
    """
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception:
        df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")

    parent_col = "(Parent) ASIN"
    if parent_col not in df.columns:
        raise ValueError("Could not find '(Parent) ASIN' column. Please upload Amazon Business Report (By ASIN).")

    # Aggregate by parent ASIN
    parents: dict[str, dict] = {}

    for _, row in df.iterrows():
        parent = str(row.get(parent_col, "")).strip()
        if not parent or len(parent) < 10:
            continue

        sessions = _clean_number(row.get("Sessions - Total", 0))
        units = _clean_number(row.get("Units Ordered", 0))
        revenue = _clean_number(row.get("Ordered Product Sales", 0))
        conv = _clean_number(row.get("Unit Session Percentage", 0))
        buybox = _clean_number(row.get("Featured Offer Percentage", 0))
        title = str(row.get("Title", "")).strip()
        total_orders = _clean_number(row.get("Total Order Items", 0))

        if parent in parents:
            p = parents[parent]
            p["sessions"] += sessions
            p["units_ordered"] += int(units)
            p["revenue"] += revenue
            p["total_order_items"] += int(total_orders)
            p["_conv_sum"] += conv
            p["_buybox_sum"] += buybox
            p["_row_count"] += 1
            # Keep first title (usually the canonical one)
        else:
            parents[parent] = {
                "parent_asin": parent,
                "title": title,
                "brand": _detect_brand(title),
                "sessions": sessions,
                "units_ordered": int(units),
                "revenue": revenue,
                "total_order_items": int(total_orders),
                "_conv_sum": conv,
                "_buybox_sum": buybox,
                "_row_count": 1,
            }

    # Finalize averages
    result = []
    for p in parents.values():
        n = p["_row_count"]
        result.append({
            "parent_asin": p["parent_asin"],
            "title": p["title"],
            "brand": p["brand"],
            "sessions": int(p["sessions"]),
            "units_ordered": p["units_ordered"],
            "revenue": round(p["revenue"], 2),
            "total_order_items": p["total_order_items"],
            "conversion_rate": round(p["_conv_sum"] / n, 2) if n > 0 else 0,
            "buy_box_pct": round(p["_buybox_sum"] / n, 2) if n > 0 else 0,
        })

    return result


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_report(
    request: Request,
    file: UploadFile = File(...),
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Upload Business Report CSV → aggregate by Parent ASIN → enrich with DB data → return analysis."""
    content = await file.read()
    try:
        products = parse_and_aggregate(content)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Filter out Prepto (Other brand)
    products = [p for p in products if p["brand"] != "Other"]

    # Enrich with rating data from DB
    for p in products:
        asin = p["parent_asin"]
        # Get current rating + review count
        db_product = (await db.execute(
            select(Product).where(Product.asin == asin)
        )).scalar_one_or_none()

        p["rating"] = None
        p["review_count"] = None
        p["rating_30d_ago"] = None
        p["rating_decline"] = None

        if db_product:
            # Latest rating
            latest = (await db.execute(
                select(RatingHistory)
                .where(RatingHistory.product_id == db_product.id)
                .order_by(desc(RatingHistory.scraped_at))
                .limit(1)
            )).scalar_one_or_none()

            if latest:
                p["rating"] = float(latest.rating) if latest.rating else None
                p["review_count"] = latest.rating_count

            # Rating from ~30 days ago
            cutoff = datetime.utcnow() - timedelta(days=25)
            old_rating = (await db.execute(
                select(RatingHistory)
                .where(and_(
                    RatingHistory.product_id == db_product.id,
                    RatingHistory.scraped_at <= cutoff,
                ))
                .order_by(desc(RatingHistory.scraped_at))
                .limit(1)
            )).scalar_one_or_none()

            if old_rating and latest and latest.rating and old_rating.rating:
                p["rating_30d_ago"] = float(old_rating.rating)
                p["rating_decline"] = round(float(old_rating.rating) - float(latest.rating), 2)

    # Sort by revenue descending for the full table
    products.sort(key=lambda x: x["revenue"], reverse=True)

    # Compute bottom 5% thresholds
    n = len(products)
    bottom_n = max(1, int(n * 0.05))  # at least 1

    by_units = sorted(products, key=lambda x: x["units_ordered"])
    by_revenue = sorted(products, key=lambda x: x["revenue"])
    by_rating = sorted([p for p in products if p["rating"] is not None], key=lambda x: x["rating"])

    # Build flags
    bottom_units_asins = set(p["parent_asin"] for p in by_units[:bottom_n])
    bottom_revenue_asins = set(p["parent_asin"] for p in by_revenue[:bottom_n])
    bottom_rating_asins = set(p["parent_asin"] for p in by_rating[:bottom_n]) if by_rating else set()
    low_rating_asins = set(p["parent_asin"] for p in products if p["rating"] is not None and p["rating"] < 3.5)
    declining_rating_asins = set(p["parent_asin"] for p in products if p["rating_decline"] is not None and p["rating_decline"] >= 0.3)

    for p in products:
        flags = []
        asin = p["parent_asin"]
        if asin in bottom_units_asins:
            flags.append("bottom_5_units")
        if asin in bottom_revenue_asins:
            flags.append("bottom_5_revenue")
        if asin in bottom_rating_asins:
            flags.append("bottom_5_rating")
        if asin in low_rating_asins:
            flags.append("rating_below_3.5")
        if asin in declining_rating_asins:
            flags.append("rating_declining")
        p["flags"] = flags

    return JSONResponse({
        "total_parents": n,
        "products": products,
        "bottom_5pct_count": bottom_n,
        "summary": {
            "total_units": sum(p["units_ordered"] for p in products),
            "total_revenue": round(sum(p["revenue"] for p in products), 2),
            "avg_conversion": round(sum(p["conversion_rate"] for p in products) / n, 2) if n > 0 else 0,
            "mithila_count": sum(1 for p in products if p["brand"] == "Mithila Foods"),
            "howrah_count": sum(1 for p in products if p["brand"] == "Howrah Foods"),
        },
        "alerts": {
            "bottom_5_units": [p for p in products if p["parent_asin"] in bottom_units_asins],
            "bottom_5_revenue": [p for p in products if p["parent_asin"] in bottom_revenue_asins],
            "bottom_5_rating": [p for p in products if p["parent_asin"] in bottom_rating_asins],
            "rating_below_3_5": [p for p in products if p["parent_asin"] in low_rating_asins],
            "rating_declining": [p for p in products if p["parent_asin"] in declining_rating_asins],
        },
    })


@router.post("/download")
async def download_report(
    request: Request,
    file: UploadFile = File(...),
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Same as upload but returns Excel file."""
    content = await file.read()
    try:
        products = parse_and_aggregate(content)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    products = [p for p in products if p["brand"] != "Other"]
    products.sort(key=lambda x: x["revenue"], reverse=True)

    rows = [{
        "Parent ASIN": p["parent_asin"],
        "Brand": p["brand"],
        "Title": p["title"],
        "Sessions": p["sessions"],
        "Units Ordered": p["units_ordered"],
        "Revenue (₹)": p["revenue"],
        "Conversion %": p["conversion_rate"],
        "Buy Box %": p["buy_box_pct"],
    } for p in products]

    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Portfolio Review")
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Portfolio_Review.xlsx"},
    )
