"""The Orders tab: today's picking sheet, and the order list for reconciliation.

Asked for: *"after the orders are packed and shipped on the portal my warehouse team is
able to reconcile the data. the orders which have to be shipped today. item wise weight
wise qty totalled. total number of orders of each item and total orders."*

**Nothing here calls Amazon.** Every route reads local rows, because `getOrders` allows one
request every 22 seconds — a page that called it would hang, and two people opening the tab
would 429. `POST /refresh` starts the background job and returns immediately; the screen
polls `/refresh-status`.

Read-only by construction: no route writes an order. These rows are a cache of Amazon's
data, so a wrong value is fixed by refreshing, not editing — a local edit would create a
second source of truth about whether an order shipped.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import permissions
from app.database import get_db
from app.orders import logic, refresh, repository
from app.routers.auth import require_area
from app.shipment import catalogue, documents

router = APIRouter(prefix="/orders")
logger = logging.getLogger(__name__)

#: Retention and the default window. 90 days matches DATA_RETENTION_DAYS and the rest of
#: the app rather than introducing a second retention concept.
WINDOW_DAYS = 90

#: The section headings the warehouse reads, and the order they are worked in. Defined here
#: rather than in the template so the Excel export and the screen cannot disagree.
SECTION_LABELS = {
    logic.BUCKET_TODAY: "To pack & ship",
    logic.BUCKET_PICKUP: "Waiting for pickup",
    logic.BUCKET_LATER: "Later",
}


async def _sheet_and_orders(db: AsyncSession):
    """The picking sheet, the order rows behind it, and where the catalogue came from.

    ONE function, so the screen and the export are computed identically. Two call sites
    aggregating separately is how a printed sheet and a screen start disagreeing about a
    quantity — the failure the shipment feature's single ORDER BY exists to prevent.

    Today is taken in IST at call time, never stored: a sheet opened tomorrow must be
    correct without a refresh having run.
    """
    orders = await repository.load_orders(db, days=WINDOW_DAYS)
    sheet_catalogue, warning, source = await catalogue.load_catalogue()
    today = datetime.now(logic.IST).date()
    sheet = logic.picking_sheet(orders, sheet_catalogue, today)
    return sheet, orders, {"source": source, "warning": warning, "today": today.isoformat()}


@router.get("")
async def list_orders(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """The picking sheet plus the flat order list. Local rows only — no Amazon call.

    Every timestamp is rendered IST here, once, so no client has to know the offset. The
    stored values stay UTC; see `orders.logic.to_ist` for why that split matters.
    """
    sheet, orders, meta = await _sheet_and_orders(db)

    rows = []
    today = datetime.now(logic.IST).date()
    for order in orders:
        due = logic.ship_by_date(order)
        purchased = logic.to_ist(order["purchase_date_utc"])
        rows.append({
            "amazon_order_id": order["amazon_order_id"],
            "purchase_date_ist": purchased.isoformat() if purchased else None,
            # None when Amazon sent its 1995 sentinel, which must never render as a date.
            "ship_by_ist": due.isoformat() if due else None,
            "overdue": bool(due and due < today and order["status"] in logic.OPEN_ORDER),
            "status": order["status"],
            "easyship_status": order["easyship_status"],
            "bucket": logic.bucket_for(order, today),
            "order_total": order["order_total"],
            "currency": order["currency"],
            "is_cod": order["is_cod"],
            "is_prime": order["is_prime"],
            "city": order["city"],
            "state": order["state"],
            "items": order["items"],
        })

    last = await repository.last_refreshed_at(db)
    return JSONResponse({
        "sheet": sheet,
        "section_labels": SECTION_LABELS,
        "orders": rows,
        "total_orders": len(rows),
        "last_refreshed_at": last.isoformat() if last else None,
        "catalogue_source": meta["source"],
        "catalogue_warning": meta["warning"],
        "today_ist": meta["today"],
        "refresh": refresh.status(),
        "window_days": WINDOW_DAYS,
    })


@router.post("/refresh")
async def start_refresh(
    request: Request,
    grant=Depends(require_area(permissions.ORDERS)),
):
    """Start the background refresh and return at once.

    **Does not await the job.** A full refresh pages `getOrders` at one call every 22
    seconds, so awaiting it would hold the request open for minutes and time out behind
    Caddy. The screen polls `/refresh-status`.

    A second call while one runs is refused by `refresh.run` itself rather than by a check
    here — the guard belongs with the state it protects, so every caller inherits it.
    """
    import asyncio

    if refresh.status().get("running"):
        return JSONResponse(
            {"error": "A refresh is already running.", "refresh": refresh.status()},
            status_code=409,
        )

    # Fire and forget. The task holds its own session: the request's session closes when
    # this handler returns, so using it would fail once the response was sent.
    asyncio.create_task(refresh.run(days=WINDOW_DAYS))
    return JSONResponse({"started": True, "refresh": refresh.status()})


@router.get("/refresh-status")
async def refresh_status(
    request: Request,
    grant=Depends(require_area(permissions.ORDERS)),
):
    """Live progress for the banner. Cheap enough to poll every few seconds."""
    return JSONResponse(refresh.status())


@router.get("/download/picking-sheet.xlsx")
async def download_picking_sheet(
    request: Request,
    bucket: str = logic.BUCKET_TODAY,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """The picking sheet as Excel, for the section the warehouse is working.

    Built through the same `_sheet_and_orders` the screen uses, so the printed sheet and the
    screen cannot disagree about a quantity. Uses the existing
    `documents.build_simple_xlsx`, so this file looks like every other document the app
    produces rather than a stranger.
    """
    if bucket not in SECTION_LABELS:
        return JSONResponse(
            {"error": f"Unknown section {bucket!r}."}, status_code=400
        )

    sheet, _orders, meta = await _sheet_and_orders(db)
    section = sheet["sections"][bucket]

    rows = [
        [
            line["product"],
            line["weight_label"],
            line["brand"],
            line["quantity"],
            line["orders"],
            # An unweighed line shows blank, not 0: 0 kg would read as a measurement.
            line["kg"] if line["kg"] is not None else "",
        ]
        for line in section["lines"]
    ]
    totals = section["totals"]
    subtitle = (
        f"{SECTION_LABELS[bucket]} · {meta['today']} (IST) · "
        f"{totals['orders']} orders · {totals['quantity']} units · {totals['kg']} kg net"
    )
    if totals["lines_without_weight"]:
        subtitle += f" · {totals['lines_without_weight']} line(s) with no pack size"

    stream = documents.build_simple_xlsx(
        "Picking sheet",
        subtitle,
        ["Product", "Size", "Brand", "Units", "Orders", "Net kg"],
        rows,
        [34, 10, 8, 10, 10, 12],
    )
    filename = f"picking-sheet-{bucket}-{meta['today']}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
