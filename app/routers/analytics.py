"""Portfolio -> Analytics. Reads stored months; only the explicit Refresh calls Amazon (read-only)."""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import ist, permissions
from app.analytics import refresh, service
from app.database import get_db
from app.routers.auth import require_area

router = APIRouter(prefix="/portfolio/analytics", tags=["analytics"])
MEDIA = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf": "application/pdf"}


@router.get("")
async def analytics(brand: str | None = None, category: str | None = None, include_inactive: bool = False,
                    period_end: str | None = None, span: int = 1,
                    db: AsyncSession = Depends(get_db), grant=Depends(require_area(permissions.PORTFOLIO))):
    return await service.build(db, brand=brand, category=category or None, include_inactive=include_inactive,
                               period_end=period_end, span=span)


@router.post("/refresh")
async def start_refresh(grant=Depends(require_area(permissions.PORTFOLIO))):
    """The current and previous month, in the background (a few minutes)."""
    if refresh.STATE["running"]:
        return JSONResponse({"error": "An analytics refresh is already running."}, status_code=409)
    asyncio.create_task(refresh.run_recent(source="button"))
    return {"started": True}


@router.get("/refresh-status")
async def refresh_status(grant=Depends(require_area(permissions.PORTFOLIO))):
    return refresh.STATE


def _table(data: dict):
    from app.portfolio.export import Column, Row, Table
    last = data.get("last_month") or ""
    cols = [Column("product", "Product", "text"), Column("brand", "Brand", "text"),
            Column("category", "Category", "text"), Column("sales", f"Sales {last}", "money"),
            Column("mom", "MoM %", "pct_signed"), Column("kg", "Kg sold", "kg"),
            Column("net_pct", "Net %", "pct_signed"), Column("price_pack", "Price of pack", "text"),
            Column("price_now", "Price now", "money"), Column("price_6m", "Price 6 months ago", "money"),
            Column("price_low", "Lowest month", "money"), Column("price_high", "Highest month", "money"),
            Column("sessions", "Sessions", "int"), Column("conversion", "Conversion %", "pct"),
            Column("buy_box", "Buy Box %", "pct")]
    rows = [Row("sku", 0, False, {c.id: p.get(c.id) for c in cols}) for p in data["products"]]
    t = (data["totals"].get(last) or {})
    totals = {"product": "Total", "sales": t.get("sales"), "mom": t.get("mom"), "kg": t.get("kg"),
              "net_pct": t.get("net_pct"), "sessions": t.get("sessions"), "conversion": t.get("conversion"),
              "buy_box": t.get("buy_box")}
    return Table(cols, rows, "Total", totals)


@router.post("/export")
async def export(body: dict = Body(default={}), db: AsyncSession = Depends(get_db),
                 grant=Depends(require_area(permissions.PORTFOLIO))):
    from app.portfolio import export as pf_export
    fmt = body.get("format") if body.get("format") in MEDIA else "xlsx"
    data = await service.build(db, brand=body.get("brand"), category=body.get("category") or None,
                               include_inactive=bool(body.get("include_inactive")))
    table = _table(data)
    slug = "-".join(x for x in (re.sub(r"[^a-z0-9]+", "-", (body.get("category") or "").lower()).strip("-"),
                                ist.today().isoformat()) if x)
    if fmt == "xlsx":
        out = pf_export.build_xlsx(table, "Analytics", freeze="B3")
    else:
        out = pf_export.build_pdf(table, f"Analytics · {data.get('brand')} · {data.get('category') or 'all categories'}"
                                         f" · {data.get('last_month') or ''}")
    return StreamingResponse(out, media_type=MEDIA[fmt],
                             headers={"Content-Disposition": f'attachment; filename="analytics-{slug}.{fmt}"'})
