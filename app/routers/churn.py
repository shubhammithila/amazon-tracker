"""Portfolio Review — aggregate Business Report by parent product family, persist last upload."""
import io
import json
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Request, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_

from app.database import get_db
from app.models import Product, RatingHistory
from app.routers.auth import require_auth

router = APIRouter(prefix="/churn")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
FAMILIES_FILE = BASE_DIR / "invoice" / "product_families.json"
LAST_REPORT_FILE = BASE_DIR.parent / "portfolio_report.json"


def load_product_families() -> dict:
    """Load ASIN -> {parent_product, brand, weight} mapping."""
    if FAMILIES_FILE.exists():
        with open(FAMILIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


PRODUCT_FAMILIES = load_product_families()


# ─── CSV Parsing ─────────────────────────────────────────────────────────────

def _clean_number(val) -> float:
    if val is None or str(val).strip() in ("", "-", "nan"):
        return 0.0
    cleaned = re.sub(r"[₹,%\s]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_and_aggregate(content: bytes) -> list[dict]:
    """
    Parse Business Report CSV. Group by parent product using product_families.json.
    Sums: sessions, units_ordered, revenue, total_order_items
    """
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception:
        df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig")

    child_col = "(Child) ASIN"
    if child_col not in df.columns:
        raise ValueError("Could not find '(Child) ASIN' column. Upload Amazon Business Report (By ASIN).")

    # First pass: aggregate raw CSV rows by child ASIN (dedup FBA vs non-FBA)
    asin_data: dict[str, dict] = {}
    for _, row in df.iterrows():
        asin = str(row.get(child_col, "")).strip()
        if not asin or len(asin) < 10:
            continue

        sessions = _clean_number(row.get("Sessions - Total", 0))
        units = _clean_number(row.get("Units Ordered", 0))
        revenue = _clean_number(row.get("Ordered Product Sales", 0))
        conv = _clean_number(row.get("Unit Session Percentage", 0))
        buybox = _clean_number(row.get("Featured Offer Percentage", 0))
        title = str(row.get("Title", "")).strip()

        if asin in asin_data:
            asin_data[asin]["sessions"] += sessions
            asin_data[asin]["units"] += int(units)
            asin_data[asin]["revenue"] += revenue
        else:
            asin_data[asin] = {
                "sessions": sessions,
                "units": int(units),
                "revenue": revenue,
                "conversion": conv,
                "buybox": buybox,
                "title": title,
            }

    # Second pass: group by parent product family
    families: dict[str, dict] = {}
    unmapped_asins = []

    for asin, data in asin_data.items():
        family_info = PRODUCT_FAMILIES.get(asin)
        if not family_info:
            unmapped_asins.append(asin)
            continue

        parent = family_info["parent_product"]
        brand = family_info["brand"]

        if parent not in families:
            families[parent] = {
                "parent_product": parent,
                "brand": brand,
                "asins": [],
                "sessions": 0,
                "units_ordered": 0,
                "revenue": 0.0,
                "variations": 0,
                "_conv_total": 0.0,
                "_conv_count": 0,
            }

        f = families[parent]
        f["asins"].append(asin)
        f["sessions"] += data["sessions"]
        f["units_ordered"] += data["units"]
        f["revenue"] += data["revenue"]
        f["variations"] += 1
        if data["conversion"] > 0:
            f["_conv_total"] += data["conversion"]
            f["_conv_count"] += 1

    # Finalize
    result = []
    for f in families.values():
        conv_avg = round(f["_conv_total"] / f["_conv_count"], 2) if f["_conv_count"] > 0 else 0
        result.append({
            "parent_product": f["parent_product"],
            "brand": f["brand"],
            "variations": f["variations"],
            "asins": f["asins"],
            "sessions": int(f["sessions"]),
            "units_ordered": f["units_ordered"],
            "revenue": round(f["revenue"], 2),
            "avg_conversion": conv_avg,
            "rating": None,
            "review_count": None,
            "rating_30d_ago": None,
            "rating_decline": None,
        })

    return result, unmapped_asins


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_report(
    request: Request,
    file: UploadFile = File(...),
    _=Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Upload Business Report CSV -> aggregate by parent product family -> persist."""
    content = await file.read()
    try:
        products, unmapped = parse_and_aggregate(content)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Enrich with rating data from DB (use first ASIN in family as representative)
    for p in products:
        if not p["asins"]:
            continue
        # Try each ASIN until we find rating data
        for asin in p["asins"]:
            db_product = (await db.execute(
                select(Product).where(Product.asin == asin)
            )).scalar_one_or_none()
            if not db_product:
                continue

            latest = (await db.execute(
                select(RatingHistory)
                .where(RatingHistory.product_id == db_product.id)
                .order_by(desc(RatingHistory.scraped_at))
                .limit(1)
            )).scalar_one_or_none()

            if latest and latest.rating:
                p["rating"] = float(latest.rating)
                p["review_count"] = latest.rating_count

                # Rating from ~30d ago
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

                if old_rating and old_rating.rating:
                    p["rating_30d_ago"] = float(old_rating.rating)
                    p["rating_decline"] = round(float(old_rating.rating) - float(latest.rating), 2)
                break

    # Sort by revenue desc
    products.sort(key=lambda x: x["revenue"], reverse=True)

    # Compute bottom 5% alerts
    n = len(products)
    bottom_n = max(1, int(n * 0.05))

    by_units = sorted(products, key=lambda x: x["units_ordered"])
    by_revenue = sorted(products, key=lambda x: x["revenue"])
    rated = [p for p in products if p["rating"] is not None]
    by_rating = sorted(rated, key=lambda x: x["rating"])

    bottom_units = [p["parent_product"] for p in by_units[:bottom_n]]
    bottom_revenue = [p["parent_product"] for p in by_revenue[:bottom_n]]
    bottom_rating = [p["parent_product"] for p in by_rating[:bottom_n]] if by_rating else []
    low_rating = [p["parent_product"] for p in products if p["rating"] is not None and p["rating"] < 3.5]
    declining_rating = [p["parent_product"] for p in products if p["rating_decline"] is not None and p["rating_decline"] >= 0.3]

    # Add flags
    for p in products:
        flags = []
        name = p["parent_product"]
        if name in bottom_units:
            flags.append("bottom_5_units")
        if name in bottom_revenue:
            flags.append("bottom_5_revenue")
        if name in bottom_rating:
            flags.append("bottom_5_rating")
        if name in low_rating:
            flags.append("rating_below_3.5")
        if name in declining_rating:
            flags.append("rating_declining")
        p["flags"] = flags

    # Remove internal fields before saving
    for p in products:
        p.pop("asins", None)

    # Build response
    response_data = {
        "uploaded_at": datetime.utcnow().isoformat(),
        "filename": file.filename or "",
        "total_parents": n,
        "unmapped_asins": unmapped[:20],
        "summary": {
            "total_units": sum(p["units_ordered"] for p in products),
            "total_revenue": round(sum(p["revenue"] for p in products), 2),
            "avg_conversion": round(sum(p["avg_conversion"] for p in products) / n, 2) if n > 0 else 0,
            "mithila_count": sum(1 for p in products if p["brand"] == "Mithila Foods"),
            "howrah_count": sum(1 for p in products if p["brand"] == "Howrah Foods"),
        },
        "alerts": {
            "bottom_5_units": [p for p in products if p["parent_product"] in bottom_units],
            "bottom_5_revenue": [p for p in products if p["parent_product"] in bottom_revenue],
            "bottom_5_rating": [p for p in products if p["parent_product"] in bottom_rating],
            "rating_below_3_5": [p for p in products if p["parent_product"] in low_rating],
            "rating_declining": [p for p in products if p["parent_product"] in declining_rating],
        },
        "products": products,
    }

    # Persist as last report (overwrite)
    try:
        with open(LAST_REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(response_data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Could not save report: {e}")

    return JSONResponse(response_data)


@router.get("/last-report")
async def get_last_report(request: Request, _=Depends(require_auth)):
    """Return the last uploaded portfolio report (persisted on disk)."""
    if not LAST_REPORT_FILE.exists():
        return JSONResponse({"products": [], "total_parents": 0})

    with open(LAST_REPORT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return JSONResponse(data)


@router.post("/download")
async def download_report(
    request: Request,
    _=Depends(require_auth),
):
    """Download last report as Excel."""
    if not LAST_REPORT_FILE.exists():
        return JSONResponse({"error": "No report uploaded yet"}, status_code=404)

    with open(LAST_REPORT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    products = data.get("products", [])
    rows = [{
        "Parent Product": p["parent_product"],
        "Brand": p["brand"],
        "Variations": p["variations"],
        "Sessions (30d)": p["sessions"],
        "Units Ordered (30d)": p["units_ordered"],
        "Revenue (INR)": p["revenue"],
        "Avg Conversion %": p["avg_conversion"],
        "Rating": p.get("rating") or "",
        "Reviews": p.get("review_count") or "",
        "Rating Change": p.get("rating_decline") or "",
        "Flags": ", ".join(p.get("flags", [])),
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
