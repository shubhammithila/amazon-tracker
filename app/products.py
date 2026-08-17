"""Product pricing: purchase rate, HSN code and GST rate per ASIN.

The Products tab writes here, and the shipment and invoice flows read here.

**Why a table and not the JSON file it supersedes.** ``app/invoice/pricing_data.json`` holds
410 rates and is tracked in git. Writing to it at runtime would recreate the problem
``hsn_master.json`` already causes on every deploy — that file has to be stashed and
restored by hand or a checkout silently reverts data the owner typed. Rows survive a
checkout; a tracked JSON file does not.

**The JSON stays as a fallback, and that is deliberate.** 198 ASIN-keyed rates were seeded
into the table by migration, but the file also carries 212 SKU-keyed entries for products
whose ASIN was never in it. Dropping the fallback would silently lose those, so a lookup
tries the table first and the file second. The file is never written.

One cache, one invalidation point. The lookups are called per invoice line and per shipment
line, so they must not hit the database on every call — but a price the owner just typed has
to take effect immediately, which is why ``invalidate()`` runs on every write rather than
relying on a TTL.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProductPrice

logger = logging.getLogger(__name__)

#: {ASIN: {"rate": float, "hsn": str, "gst": float}} — populated on first read.
#: None means "not loaded yet", which is different from an empty dict (loaded, no rows).
_cache: dict[str, dict] | None = None

DEFAULT_HSN = "1106"
DEFAULT_GST = 5.0


def invalidate() -> None:
    """Forget the cache. Called after every write, so an edit takes effect at once."""
    global _cache
    _cache = None


async def load_cache(db: AsyncSession) -> dict[str, dict]:
    """Every priced ASIN, cached. Cheap: ~200 rows.

    Loaded in one query rather than per lookup because a 200-line invoice would otherwise
    make 200 round trips, and the numbers are wanted while rendering a page.
    """
    global _cache
    if _cache is not None:
        return _cache

    rows = (await db.execute(select(ProductPrice))).scalars()
    built: dict[str, dict] = {}
    for row in rows:
        asin = (row.asin or "").strip().upper()
        if not asin:
            continue
        built[asin] = {
            # None stays None. A missing price is a state the shipment flow must refuse
            # by name, not a zero it can send to Amazon — a declared value of 0 is
            # rejected with "We encountered an internal error", which looks like a fault
            # on their side and is not.
            "rate": float(row.purchase_rate) if row.purchase_rate is not None else None,
            "hsn": (row.hsn_code or DEFAULT_HSN).strip(),
            "gst": float(row.gst_rate) if row.gst_rate is not None else DEFAULT_GST,
            "sku": (row.fba_sku or "").strip(),
        }
    _cache = built
    return _cache


async def rate_for(db: AsyncSession, asin: str, sku: str = "") -> float:
    """Purchase rate for one product, or 0.0 when it is not priced.

    Table first, then ``pricing_data.json``. The file still holds 212 SKU-keyed entries
    for products whose ASIN was never in it, so skipping the fallback would quietly lose
    prices that exist.

    Returns 0.0 rather than raising, because most callers only want a number for a
    document — the shipment flow checks for 0 explicitly and refuses, which is where a
    missing price actually matters.
    """
    cache = await load_cache(db)
    entry = cache.get((asin or "").strip().upper())
    if entry and entry["rate"]:
        return entry["rate"]

    from app.invoice.parser import get_purchase_rate

    return float(get_purchase_rate(sku or "", asin or "") or 0)


async def tax_for(db: AsyncSession, asin: str) -> dict:
    """HSN code and GST rate for one product, falling back to 1106 at 5%.

    Every F2D product is 1106 at 5% today, which is why that is the default rather than an
    error — but it is stored per product so a non-food line can differ without a code
    change.
    """
    cache = await load_cache(db)
    entry = cache.get((asin or "").strip().upper())
    if entry:
        return {"hsn_code": entry["hsn"], "gst_rate": entry["gst"]}
    return {"hsn_code": DEFAULT_HSN, "gst_rate": DEFAULT_GST}


async def upsert(
    db: AsyncSession,
    asin: str,
    *,
    purchase_rate=None,
    hsn_code: str | None = None,
    gst_rate=None,
    item: str | None = None,
    fba_sku: str | None = None,
    weight=None,
    brand: str | None = None,
) -> ProductPrice:
    """Create or update one product's pricing. Keyword-only past the ASIN.

    Every editable field is optional and ``None`` means "leave alone", which is what lets
    the screen save one cell without sending the whole row — and stops a blank field in a
    partial payload wiping a price that is already correct.

    An empty-string price is distinct from ``None``: it means "clear this", so a rate typed
    by mistake can be removed. That is why the caller passes ``""`` rather than 0 — a 0
    price would be sent to Amazon and rejected.
    """
    asin = (asin or "").strip().upper()
    if not asin:
        raise ValueError("asin is required")

    row = (
        await db.execute(select(ProductPrice).where(ProductPrice.asin == asin))
    ).scalar_one_or_none()
    if row is None:
        row = ProductPrice(asin=asin)
        db.add(row)

    if purchase_rate is not None:
        row.purchase_rate = None if purchase_rate == "" else Decimal(str(purchase_rate))
    if hsn_code is not None:
        row.hsn_code = hsn_code.strip() or DEFAULT_HSN
    if gst_rate is not None:
        row.gst_rate = None if gst_rate == "" else Decimal(str(gst_rate))
    if item is not None:
        row.item = item
    if fba_sku is not None:
        row.fba_sku = fba_sku
    if weight is not None:
        row.weight = Decimal(str(weight)) if weight != "" else None
    if brand is not None:
        row.brand = brand

    await db.commit()
    await db.refresh(row)
    invalidate()
    return row
