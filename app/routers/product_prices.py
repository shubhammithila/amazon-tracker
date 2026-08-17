"""The Products tab: purchase rate, HSN and GST per product.

Asked for: *"lets add a products tab separately where I can update the pricing. Jitne
products ka pricing hai abhi wo daal do. and rest ka blank chor do."*

So the list is the **catalogue**, not the price table: every product that exists gets a row,
priced or not, because the point of the screen is to find and fill the blanks. 198 rates
were seeded by migration; the rest read blank until the owner types them.

Admin only. A purchase rate is the cost side of the business, and the accounts preset
deliberately excludes projections and purchase costs for the same reason.
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import products
from app.database import get_db
from app.models import ProductPrice
from app.routers.auth import require_admin
from app.shipment import catalogue

router = APIRouter(prefix="/products-pricing")
logger = logging.getLogger(__name__)


def _weight_label(weight) -> str:
    """"500g" / "1 kg", matching every other screen.

    Grams tight, kilos spaced — the same rule ``logic.weight_label`` uses, imported rather
    than reimplemented so the Products tab cannot drift from the packing sheet.
    """
    from app.shipment.logic import weight_label

    return weight_label(weight)


@router.get("")
async def list_products(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Every catalogue product with its price, HSN and GST. Unpriced ones included.

    Built from the **catalogue** — the live MRP sheet, falling back to its cached copy and
    then the static file — so a product that exists but has never been priced still appears.
    A list built from the price table would show only what is already priced, which is the
    opposite of what this screen is for.

    Rows the catalogue does not know about are still listed if they carry a price: an ASIN
    can leave the sheet while its historical price remains meaningful, and silently dropping
    it would look like data loss.
    """
    sheet, warning, source = await catalogue.load_catalogue()
    priced = await products.load_cache(db)

    rows = []
    seen = set()
    for asin, info in sheet.items():
        key = asin.strip().upper()
        seen.add(key)
        entry = priced.get(key) or {}
        brand = str(info.get("brand") or "")
        rows.append({
            "asin": key,
            "item": info.get("name") or "",
            "weight": float(info.get("weight") or 0),
            "weight_label": _weight_label(info.get("weight")),
            "brand": "MF" if "mithila" in brand.lower() else ("HF" if brand else ""),
            "active": bool(info.get("active")),
            "purchase_rate": entry.get("rate"),
            "hsn_code": entry.get("hsn") or products.DEFAULT_HSN,
            "gst_rate": entry.get("gst") if entry else products.DEFAULT_GST,
            "in_catalogue": True,
        })

    # Priced ASINs the catalogue no longer lists. Kept visible rather than dropped.
    for asin, entry in priced.items():
        if asin in seen:
            continue
        rows.append({
            "asin": asin,
            "item": "",
            "weight": 0,
            "weight_label": "",
            "brand": "",
            "active": False,
            "purchase_rate": entry.get("rate"),
            "hsn_code": entry.get("hsn") or products.DEFAULT_HSN,
            "gst_rate": entry.get("gst", products.DEFAULT_GST),
            "in_catalogue": False,
        })

    # Unpriced ACTIVE products first, then unpriced inactive ones, then everything priced.
    # The ordering is the feature: 72 products have no price but only 8 are still sold, and
    # those 8 are the ones that will refuse a shipment. Sorting alphabetically — or even
    # just "unpriced first" — would bury them among 64 discontinued lines and turn an
    # eight-item job into a list nobody finishes.
    rows.sort(key=lambda r: (
        r["purchase_rate"] is not None,
        not r["active"],
        (r["item"] or "").casefold(),
        r["weight"],
    ))

    missing = [r for r in rows if r["purchase_rate"] is None]
    missing_active = [r for r in missing if r["active"]]
    return JSONResponse({
        "products": rows,
        "total": len(rows),
        "missing_price": len(missing),
        # Counted separately because it is the number that matters. A price is only needed
        # for a product that can still be shipped; the rest are history.
        "missing_price_active": len(missing_active),
        "catalogue_source": source,
        "catalogue_warning": warning,
    })


@router.post("/save")
async def save_product_price(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Save one product's pricing.

    Body: ``{"asin": "B0…", "purchase_rate": 65, "hsn_code": "1106", "gst_rate": 5}``.

    Every field except the ASIN is optional, and an omitted field is left alone — so the
    screen can save one cell without risking the others. An explicit empty string clears
    the price, which is how a rate typed by mistake is removed; 0 is refused, because 0 is
    not a price and Amazon rejects a declared value of 0 with a message that looks like a
    fault on their side.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object."}, status_code=400)

    asin = str(body.get("asin") or "").strip().upper()
    if not asin:
        return JSONResponse({"error": "asin is required."}, status_code=400)

    rate = body.get("purchase_rate", None)
    if rate is not None and rate != "":
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "The purchase rate must be a number."}, status_code=400
            )
        if rate <= 0:
            return JSONResponse(
                {
                    "error": "A purchase rate must be more than 0. Amazon rejects an "
                    "inbound shipment whose declared value is zero, and it does so with a "
                    "message that looks like a fault on their side. Leave it blank instead "
                    "if the price is not known yet."
                },
                status_code=400,
            )

    gst = body.get("gst_rate", None)
    if gst is not None and gst != "":
        try:
            gst = float(gst)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "The GST rate must be a number."}, status_code=400
            )
        if not 0 <= gst <= 100:
            return JSONResponse(
                {"error": "A GST rate must be between 0 and 100."}, status_code=400
            )

    hsn = body.get("hsn_code", None)
    if hsn is not None:
        hsn = str(hsn).strip()
        # HSN codes are 4, 6 or 8 digits. A typo here reaches a GST document and an
        # Amazon inbound declaration, so it is checked rather than trusted.
        if hsn and (not hsn.isdigit() or len(hsn) not in (4, 6, 8)):
            return JSONResponse(
                {"error": f"{hsn!r} is not a valid HSN code — it must be 4, 6 or 8 digits."},
                status_code=400,
            )

    row = await products.upsert(
        db, asin,
        purchase_rate=rate,
        hsn_code=hsn,
        gst_rate=gst,
        item=body.get("item"),
        fba_sku=body.get("fba_sku"),
        weight=body.get("weight"),
        brand=body.get("brand"),
    )
    return JSONResponse({
        "asin": row.asin,
        "purchase_rate": float(row.purchase_rate) if row.purchase_rate is not None else None,
        "hsn_code": row.hsn_code,
        "gst_rate": float(row.gst_rate) if row.gst_rate is not None else None,
        "saved": True,
    })


@router.post("/save-many")
async def save_many(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Save several products at once, for the "Save all" button.

    Each row is validated the same way as a single save, and a bad row is REPORTED rather
    than silently skipped — a screen that says "saved" while dropping two rows is how wrong
    prices reach a GST document.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body must be JSON."}, status_code=400)

    updates = (body or {}).get("products") or []
    if not isinstance(updates, list):
        return JSONResponse({"error": "products must be a list."}, status_code=400)

    saved, failed = 0, []
    for raw in updates:
        if not isinstance(raw, dict):
            failed.append({"asin": "", "error": "not an object"})
            continue
        asin = str(raw.get("asin") or "").strip().upper()
        rate = raw.get("purchase_rate", None)
        if rate is not None and rate != "":
            try:
                rate = float(rate)
                if rate <= 0:
                    raise ValueError("must be more than 0")
            except (TypeError, ValueError) as exc:
                failed.append({"asin": asin, "error": f"purchase rate: {exc}"})
                continue
        try:
            await products.upsert(
                db, asin,
                purchase_rate=rate,
                hsn_code=raw.get("hsn_code"),
                gst_rate=raw.get("gst_rate"),
                item=raw.get("item"),
                weight=raw.get("weight"),
                brand=raw.get("brand"),
            )
            saved += 1
        except Exception as exc:                    # noqa: BLE001 - reported, not hidden
            failed.append({"asin": asin, "error": str(exc)[:120]})

    return JSONResponse({"saved": saved, "failed": failed})
