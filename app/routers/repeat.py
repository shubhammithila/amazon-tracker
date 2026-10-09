"""Portfolio -> Repeat customers. Reads stored rows; only the explicit Refresh calls Amazon."""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import ist, permissions
from app.database import get_db
from app.portfolio import export as pf_export
from app.repeat import export, refresh, service, value_service
from app.routers.auth import require_area

router = APIRouter(prefix="/portfolio/repeat", tags=["repeat"])


@router.get("")
async def repeat(brand: str | None = None, db: AsyncSession = Depends(get_db),
                 grant=Depends(require_area(permissions.PORTFOLIO))):
    return await service.build_payload(db, brand, ist.today())


@router.post("/refresh")
async def start_refresh(grant=Depends(require_area(permissions.PORTFOLIO))):
    if refresh.STATE["running"]:
        return JSONResponse({"error": "A repeat-customer refresh is already running."},
                            status_code=409)
    asyncio.create_task(refresh.run_incremental())
    return {"started": True}


@router.get("/refresh-status")
async def refresh_status(grant=Depends(require_area(permissions.PORTFOLIO))):
    return refresh.STATE


MAX_EXPORT_IDS = 5000
MEDIA = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf": "application/pdf"}


@router.post("/export")
async def export_repeat(body: dict = Body(...), db: AsyncSession = Depends(get_db),
                        grant=Depends(require_area(permissions.PORTFOLIO))):
    """The rows on screen, in the screen's order, as Excel or PDF.

    `ids` is the order the screen shows (its sort and category filter applied); the server
    rebuilds the payload and reads every number from it, so nothing numeric travels from the client.
    """
    fmt = body.get("format")
    if fmt not in MEDIA:
        return JSONResponse({"error": "format must be xlsx or pdf"}, status_code=400)
    ids = body.get("ids")
    if ids is not None and (not isinstance(ids, list) or len(ids) > MAX_EXPORT_IDS
                            or not all(isinstance(i, str) for i in ids)):
        return JSONResponse({"error": "ids must be a list of product ids"}, status_code=400)
    category = body.get("category") or None
    payload = await service.build_payload(db, body.get("brand"), ist.today())
    table = export.build_table(payload, ids, category)
    scope = category or payload.get("brand") or ""
    stem = "repeat-" + "-".join(x for x in (
        re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-"), payload.get("as_of") or "") if x)
    if fmt == "xlsx":
        data = pf_export.build_xlsx(table, "Repeat customers", freeze="B3",
                                    total_note=export.TOTAL_NOTE)
    else:
        title = f"Repeat customers · {scope}"
        subtitle = (f"FBA orders only · customers who bought in a 30-day period, followed for "
                    f"90 / 60 / 30 days · data to {payload.get('as_of') or '—'} · "
                    f"history from {payload.get('history_from') or '—'} · {len(table.rows)} product(s)")
        data = export.build_pdf(table, title, subtitle,
                                partial_below=payload.get("fba_partial_below") or 0.6)
    return StreamingResponse(data, media_type=MEDIA[fmt], headers={
        "Content-Disposition": f'attachment; filename="{stem}.{fmt}"'})


value_router = APIRouter(prefix="/portfolio/value", tags=["repeat"])


@value_router.get("")
async def customer_value(brand: str | None = None, db: AsyncSession = Depends(get_db),
                         grant=Depends(require_area(permissions.PORTFOLIO))):
    return await value_service.build_payload(db, brand, ist.today())


@value_router.post("/export")
async def export_value(body: dict = Body(...), db: AsyncSession = Depends(get_db),
                       grant=Depends(require_area(permissions.PORTFOLIO))):
    """The Customer value rows on screen, in the screen's order, as Excel or PDF. Numbers are
    rebuilt on the server; only the row order travels from the browser."""
    fmt = body.get("format")
    if fmt not in MEDIA:
        return JSONResponse({"error": "format must be xlsx or pdf"}, status_code=400)
    ids = body.get("ids")
    if ids is not None and (not isinstance(ids, list) or len(ids) > MAX_EXPORT_IDS
                            or not all(isinstance(i, str) for i in ids)):
        return JSONResponse({"error": "ids must be a list of product ids"}, status_code=400)
    category = body.get("category") or None
    payload = await value_service.build_payload(db, body.get("brand"), ist.today())
    table = value_service.build_table(payload, ids, category)
    scope = category or payload.get("brand") or ""
    stem = "customer-value-" + "-".join(x for x in (
        re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-"), payload.get("as_of") or "") if x)
    if fmt == "xlsx":
        data = pf_export.build_xlsx(table, "Customer value", freeze="B3", total_note=(
            "The brand's (or category's) own total: every customer counted once, acquired by any of "
            "its products. LTV is what customers paid, ex-GST, before product cost."))
    else:
        data = pf_export.build_pdf(table, f"Customer value · {scope} · data to "
                                          f"{payload.get('as_of') or '—'} · LTV = what customers "
                                          "paid (ex-GST), before product cost")
    return StreamingResponse(data, media_type=MEDIA[fmt], headers={
        "Content-Disposition": f'attachment; filename="{stem}.{fmt}"'})
