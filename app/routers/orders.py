"""The Orders tab: today's dispatch for the warehouse, and the picking sheet for the owner.

Asked for: *"after the orders are packed and shipped on the portal my warehouse team is
able to reconcile the data. the orders which have to be shipped today. item wise weight
wise qty totalled. total number of orders of each item and total orders."*

**Nothing here calls Amazon.** Every route reads local rows, because `getOrders` allows one
request every 22 seconds — a page that called it would hang, and two people opening the tab
would 429. `POST /refresh` starts the background job and returns immediately; the screen
polls `/refresh-status`.

**No route writes an ORDER.** Amazon's rows are a cache: a wrong value is fixed by
refreshing, not editing, because a local edit would create a second source of truth about
whether an order shipped. The one exception is `POST /packed/{pack_date}`, which writes the
warehouse's own units-packed counts — a fact Amazon does not have, and one that never
changes an order's status or reaches an invoice.
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

#: Pages the MANUAL refresh will walk, per pass. Higher than the half-hourly job's cap
#: because pressing the button means "get everything, I am watching" — but not dramatically
#: higher, because the fetch is bounded by `EasyShipShipmentStatuses` rather than by date:
#: the measured actionable set is 371 orders in 4 pages, and pages beyond the last one cost
#: 22.5 seconds each to learn nothing. This was 20 while the fetch was date-bounded.
BACKFILL_PAGES = 12

#: The section headings the warehouse reads, and the order they are worked in. Defined here
#: rather than in the template so the Excel export and the screen cannot disagree.
SECTION_LABELS = {
    logic.BUCKET_TODAY: "To pack & ship",
    logic.BUCKET_PICKUP: "Waiting for pickup",
    logic.BUCKET_LATER: "Later",
    logic.BUCKET_PENDING: "Pending payment",
}

#: Every section must be labelled, or its Excel export 400s and the heading renders as a
#: bucket key. Asserted at import rather than left to a test, because the failure is a
#: screen the warehouse cannot use and the check costs nothing.
assert set(SECTION_LABELS) >= set(logic.SHEET_SECTIONS), (
    f"unlabelled sections: {set(logic.SHEET_SECTIONS) - set(SECTION_LABELS)}"
)


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
    #
    # The FULL window and a generous page cap, unlike the half-hourly job, which looks back
    # two weeks over a few pages. This is the deep backfill: someone pressed the button
    # because they want everything, and they are watching the progress banner while it runs.
    asyncio.create_task(refresh.run(days=WINDOW_DAYS, max_pages=BACKFILL_PAGES))
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


# ─── Today's dispatch: the warehouse's screen ────────────────────────────────
#
# The picking sheet above answers the owner's question ("what state is everything in").
# These answer the floor's: "what goes out today, and how much of it have we packed."


async def _dispatch(db: AsyncSession):
    """Today's dispatch, the purchasing view, and the meta. Returns (sheet, purchasing, meta).

    ONE function behind all three tabs and all five downloads, so a printed sheet cannot
    disagree with the monitor about a quantity — the same reasoning `_sheet_and_orders`
    carries, and the reason the shipment feature funnels all five of its downloads through
    `_document_rows`.

    Today is taken in IST at call time and never stored, so a screen left open overnight is
    correct in the morning without a refresh having run.
    """
    today = datetime.now(logic.IST).date()
    pack_date = today.isoformat()
    orders = await repository.load_orders(db, days=WINDOW_DAYS)
    sheet_catalogue, warning, source = await catalogue.load_catalogue()
    packed = await repository.load_packed(db, pack_date)
    raw_stock = await repository.load_raw_stock(db)
    sheet = logic.dispatch_sheet(orders, sheet_catalogue, today, packed=packed)
    purchasing = logic.raw_stock_summary(sheet, raw_stock)
    return sheet, purchasing, {
        "source": source, "warning": warning, "today": pack_date, "pack_date": pack_date,
    }


@router.get("/dispatch")
async def dispatch(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """Today's dispatch: parent products, pack sizes, packed counts and purchasing.

    Local rows only — no Amazon call. `is_admin` travels in the payload so the screen can
    decide whether to offer the owner's other sections; the guard that actually matters is
    on the routes themselves.
    """
    sheet, purchasing, meta = await _dispatch(db)
    last = await repository.last_refreshed_at(db)
    return JSONResponse({
        "sheet": sheet,
        "purchasing": purchasing,
        "pack_date": meta["pack_date"],
        "today_ist": meta["today"],
        "catalogue_source": meta["source"],
        "catalogue_warning": meta["warning"],
        "last_refreshed_at": last.isoformat() if last else None,
        "refresh": refresh.status(),
        "is_admin": bool(getattr(grant, "is_admin", False)),
    })


@router.post("/packed/{pack_date}")
async def save_packed(
    pack_date: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """Record units packed against ASINs for today. `{"entries": [{"asin", "units"}]}`.

    **The date must be TODAY in IST, and the server decides what today is.** A browser on a
    laptop set to another timezone would otherwise file this morning's count against
    yesterday — a silent off-by-one-day that nobody would notice until the numbers were being
    reconciled. The path carries the date so the request is explicit and idempotent, not so
    the client can choose it.

    Writing the past is refused rather than silently redirected: if the screen has been open
    since before midnight, the honest answer is "reload", not "I moved your numbers".
    """
    today = datetime.now(logic.IST).date().isoformat()
    if pack_date != today:
        return JSONResponse(
            {
                "error": f"That page is for {pack_date}, but today is {today} (IST). "
                         "Reload the page before entering more counts.",
                "pack_date": today,
            },
            status_code=409,
        )

    try:
        body = await request.json()
    except Exception:                       # noqa: BLE001 - a malformed body is a 400
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)

    entries = (body or {}).get("entries")
    if not isinstance(entries, list):
        return JSONResponse(
            {"error": "entries must be a list of {asin, units} objects."}, status_code=400
        )

    packed = await repository.save_packed(db, pack_date, entries)
    # The whole map goes back, not just what was sent: the screen re-renders from the
    # committed truth rather than from what it believes it saved, because a warehouse phone
    # can lose a response and a stale total is what gets acted on.
    logger.info("orders: packed counts saved for %s (%d SKU(s))", pack_date, len(packed))
    return JSONResponse({"status": "saved", "pack_date": pack_date, "packed": packed})


@router.post("/raw-stock")
async def save_raw_stock(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """Record raw material on hand per product. `{"entries": [{"product", "raw_kg"}]}`.

    **No date in the path, unlike `/packed/{pack_date}`, and that asymmetry is the design.** A
    packed count belongs to a day; raw material on a shelf is a standing quantity, so a date
    here would make the number unreachable tomorrow and the purchasing tab would demand
    re-entry every morning.

    Kilograms rather than units: raw material is bulk, and there is no such thing as
    500 g-flavoured raw sattu.
    """
    try:
        body = await request.json()
    except Exception:                       # noqa: BLE001 - a malformed body is a 400
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)

    entries = (body or {}).get("entries")
    if not isinstance(entries, list):
        return JSONResponse(
            {"error": "entries must be a list of {product, raw_kg} objects."},
            status_code=400,
        )

    raw_stock = await repository.save_raw_stock(
        db, entries, updated_by=getattr(grant, "username", "") or ""
    )
    logger.info("orders: raw stock saved for %d product(s)", len(raw_stock))
    return JSONResponse({"status": "saved", "raw_stock": raw_stock})


@router.get("/download/dispatch.pdf")
async def download_dispatch(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """The dispatch sheet as a PDF: parent summary, then every order.

    Built through the same `_dispatch` the screen uses, so the paper and the monitor cannot
    disagree. A PDF rather than Excel because this one is read on the floor and ticked with a
    pen — the Excel downloads exist for accounts.
    """
    sheet, purchasing, meta = await _dispatch(db)
    totals = sheet["totals"]
    subtitle = (
        f"{meta['today']} (IST) · {totals['orders']} orders · {totals['units']} units · "
        f"{totals['kg']} kg net · {totals['parents']} product(s)"
    )
    if totals["sizes_without_weight"]:
        subtitle += f" · {totals['sizes_without_weight']} line(s) with no pack size"

    stream = documents.build_dispatch_pdf(sheet, subtitle)
    filename = f"dispatch-{meta['today']}.pdf"
    return StreamingResponse(
        stream,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
