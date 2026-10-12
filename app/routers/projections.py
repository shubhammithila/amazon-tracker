"""Projections: how much stock to hold, live from the Profit tab's sales.

Rebuilt 12 Oct 2026. The Business Report CSV upload, the "manual" sales mode, the per-row reset and
remove controls and the Sunday refresh job are all gone: sales are read live every time (see
`app.projections.service`), and the only things the owner edits are a product's seasonal factor and
its four lead times, plus the account-wide settings (7-day weight, growth, markup, slips).
"""
from __future__ import annotations

import io
import re

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import ist, permissions
from app.database import get_db
from app.projections import logic, repository, service
from app.routers.auth import get_current_username, require_area

router = APIRouter(prefix="/projections")

MAX_CHANGES = 500
MEDIA = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pdf": "application/pdf"}


@router.get("/last")
async def get_current(include_inactive: bool = False, db: AsyncSession = Depends(get_db),
                      grant=Depends(require_area(permissions.PROJECTIONS))):
    return await service.build(db, include_inactive=include_inactive)


@router.post("/save")
async def save(request: Request, body: dict = Body(...), db: AsyncSession = Depends(get_db),
               grant=Depends(require_area(permissions.PROJECTIONS))):
    """Store the edited cells: `{"changes": [{"key": ..., "seasonal_impact": 1.2, ...}]}`.

    Every change is checked before ANY is written, so a bad cell refuses the whole save rather than
    leaving half of it stored. Only the five editable fields are accepted; a product the screen
    did not show is refused.
    """
    changes = body.get("changes")
    if not isinstance(changes, list) or not changes:
        return JSONResponse({"error": "Nothing to save."}, status_code=400)
    if len(changes) > MAX_CHANGES:
        return JSONResponse({"error": f"At most {MAX_CHANGES} products per save."}, status_code=400)
    data = await service.build(db, include_inactive=True)
    rows = {r["key"]: r for r in data["rows"]}
    cleaned = []
    for change in changes:
        if not isinstance(change, dict) or change.get("key") not in rows:
            return JSONResponse({"error": "A changed product is not on the Projections list."},
                                status_code=400)
        row = rows[change["key"]]
        values = {}
        for field, value in change.items():
            if field == "key":
                continue
            error = logic.edit_error(field, value)
            if error:
                return JSONResponse({"error": f"{row['product']}: {error}"}, status_code=400)
            number = float(value)
            values[field] = number if field == "seasonal_impact" else int(number)
        if values:
            cleaned.append((row, values))
    user = get_current_username(request) or ""
    for row, values in cleaned:
        # A product never saved before stores the values it was SHOWN for the untouched fields,
        # so saving one lead time cannot silently reset its other three to the global defaults.
        full = {field: row[field] for field in logic.EDITABLE}
        full.update(values)
        await repository.save_config(db, row["key"], row["product"], row["brand"], full,
                                     row_id=row.get("pre_key_row"), updated_by=user)
    return await service.build(db, include_inactive=bool(body.get("include_inactive")))


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db),
                       grant=Depends(require_area(permissions.PROJECTIONS))):
    return {"settings": await repository.load_settings(db), "defaults": logic.DEFAULT_SETTINGS,
            "ranges": logic.SETTING_RANGES}


@router.post("/settings")
async def set_settings(request: Request, body: dict = Body(...), db: AsyncSession = Depends(get_db),
                       grant=Depends(require_area(permissions.PROJECTIONS))):
    """`{"seven_day_weight": 0.6, ...}`, or `{"reset": true}` for the defaults. All-or-nothing."""
    values = dict(logic.DEFAULT_SETTINGS) if body.get("reset") else {k: v for k, v in body.items()}
    for key, value in values.items():
        error = logic.setting_error(key, value)
        if error:
            return JSONResponse({"error": error}, status_code=400)
    saved = await repository.save_settings(db, values, updated_by=get_current_username(request) or "")
    return {"settings": saved}


def _table(data: dict, category: str | None, *, full: bool):
    from app.portfolio.export import Column, Row, Table
    rows = [r for r in data["rows"] if not category or r["category"] == category]
    cols = [Column("product", "Product", "text"), Column("brand", "Brand", "text"),
            Column("category", "Category", "text")]
    if full:
        cols += [Column("kg_30d", "30-day sales", "kg"), Column("kg_7d", "7-day sales", "kg"),
                 Column("projection_30d", "Projection, next 30 days", "kg"),
                 Column("seasonal_impact", "Seasonal", "times"),
                 Column("supplier_to_wh", "Supplier→WH (d)", "int"), Column("packing", "Packing (d)", "int"),
                 Column("wh_to_ixd", "WH→IXD (d)", "int"), Column("ixd_to_fba", "IXD→FBA (d)", "int"),
                 Column("lead_total", "Lead total (d)", "days"), Column("buffer_days", "WH buffer (d)", "days"),
                 Column("ideal_wh", "Ideal WH", "kg")]
    cols += [Column("ideal_fba", "Ideal FBA", "kg"), Column("total_stock", "Total stock to hold", "kg")]
    pick = lambda r: {c.id: r.get(c.id) for c in cols}  # noqa: E731
    total = logic.sum_rows(rows)
    label = f"{category or 'All products'} — total"
    return Table(cols, [Row("sku", 0, False, pick(r)) for r in rows], label,
                 {**{c.id: total.get(c.id) for c in cols}, "product": label}), len(rows)


async def _download(db, fmt: str, category: str | None, include_inactive: bool, *, full: bool):
    from app.portfolio import export as pf_export
    data = await service.build(db, include_inactive=include_inactive)
    table, count = _table(data, category, full=full)
    stem = ("projections" if full else "stock-to-hold") + "-" + "-".join(x for x in (
        re.sub(r"[^a-z0-9]+", "-", (category or "").lower()).strip("-"), ist.today().isoformat()) if x)
    sheet = "Projections" if full else "Stock to hold"
    if fmt == "xlsx":
        body = pf_export.build_xlsx(table, sheet, freeze="B3")
    else:
        w = data["windows"]
        body = pf_export.build_pdf(table, f"{sheet} · {category or 'all products'} · sales "
                                          f"{w['30'][0]} → {w['30'][1]} · {count} product(s)")
    return StreamingResponse(body, media_type=MEDIA[fmt],
                             headers={"Content-Disposition": f'attachment; filename="{stem}.{fmt}"'})


@router.get("/download/reorder.{fmt}")
async def download_reorder(fmt: str, category: str | None = None, include_inactive: bool = False,
                           db: AsyncSession = Depends(get_db),
                           grant=Depends(require_area(permissions.PROJECTIONS))):
    """For the purchase team: product, brand, Ideal FBA and Total stock to hold."""
    if fmt not in MEDIA:
        return JSONResponse({"error": "format must be xlsx or pdf"}, status_code=400)
    return await _download(db, fmt, category, include_inactive, full=False)


@router.get("/download/table.{fmt}")
async def download_table(fmt: str, category: str | None = None, include_inactive: bool = False,
                         db: AsyncSession = Depends(get_db),
                         grant=Depends(require_area(permissions.PROJECTIONS))):
    """The whole table as on screen."""
    if fmt not in MEDIA:
        return JSONResponse({"error": "format must be xlsx or pdf"}, status_code=400)
    return await _download(db, fmt, category, include_inactive, full=True)
