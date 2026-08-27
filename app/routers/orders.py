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

#: Pages the MANUAL refresh will walk, per pass. Higher than the half-hourly job's cap because
#: pressing the button means "get everything, I am watching", and its window is wider.
BACKFILL_PAGES = 8

#: Days the MANUAL refresh looks back. The half-hourly job asks only for today (see
#: `scheduler.ORDER_REFRESH_DAYS`), which is what makes it fast and is enough for the dispatch
#: screen — an order dispatched today was updated today.
#:
#: The button is the catch-up: three days covers a weekend of stragglers and an order whose
#: status settled late, at a measured cost of ~7 pages (about 2.5 minutes). Deliberately NOT 90
#: days: `PickedUp` is now in the filter and has months of history, and `getOrders` pages
#: oldest-first, so a wide window spends its whole budget on orders that are long gone. Use the
#: one-off backfill in CLAUDE.md if history genuinely needs repairing.
BACKFILL_DAYS = 3

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

#: Download variants both formats offer: the combined file, or one screen tab. Validated
#: against this set so a typo cannot silently export the wrong section — the same guard
#: `download_picking_sheet` applies to its bucket.
DOWNLOAD_TABS = ("all", "weight", "sku", "orders")

#: `tobuy` is Excel-only: it is pasted into a supplier email rather than read at a bench, and
#: it holds only the products that are short.
XLSX_ONLY_TABS = ("tobuy",)


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
    # A WIDER window than the half-hourly job, which asks only for today — this is the catch-up
    # for a straggler whose status settled late.
    #
    # `BACKFILL_DAYS`, not `WINDOW_DAYS`: the 90-day figure is how far back the SCREEN reads
    # LOCAL rows, and passing it here made the button spend its entire budget paging months-old
    # orders oldest-first. Measured: 8 pages, about three minutes, and not one order due today.
    asyncio.create_task(refresh.run(days=BACKFILL_DAYS, max_pages=BACKFILL_PAGES))
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
    order_packed = await repository.load_order_packed(db, pack_date)
    sheet = logic.dispatch_sheet(orders, sheet_catalogue, today, packed=packed)
    purchasing = logic.raw_stock_summary(sheet, raw_stock)
    return sheet, purchasing, {
        "source": source, "warning": warning, "today": pack_date, "pack_date": pack_date,
        "order_packed": order_packed,
    }


def _dispatch_subtitle(sheet: dict, meta: dict) -> str:
    """The provenance line every dispatch document carries.

    Stated on every file rather than left to whoever prints it: a sheet with no date gets worked
    from tomorrow, and these numbers change every few minutes as counts are entered.
    """
    totals = sheet["totals"]
    parts = [
        f"{meta['today']} (IST)",
        f"{totals['orders']} orders",
        f"{totals['units']} units",
        f"{totals['kg']} kg net",
        f"{totals['parents']} product(s)",
    ]
    if totals["packed"]:
        parts.append(f"{totals['packed']} packed")
    if totals["sizes_without_weight"]:
        parts.append(f"{totals['sizes_without_weight']} line(s) with no pack size")
    return " · ".join(parts)


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
        # Which ORDERS are ticked as fully packed, keyed on Amazon's order id. Distinct from the
        # per-ASIN unit counts inside `sheet`: an order with two products contributes to two
        # ASIN rows and neither knows the parcel is incomplete.
        "order_packed": meta["order_packed"],
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


@router.post("/order-packed/{pack_date}")
async def save_order_packed(
    pack_date: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """Tick orders as fully packed. `{"entries": [{"amazon_order_id", "packed", "source"}]}`.

    A different question from `POST /packed/{pack_date}`, which counts UNITS per ASIN. This
    records that one ORDER is finished — the unit that actually ships. An order holding two
    products contributes to two ASIN rows and neither one knows the parcel is incomplete, so
    without this a two-item order can be packed, looked complete and go out short.

    **The date must be TODAY in IST and the server decides what today is**, identically to
    `save_packed` and for the same reason: a laptop in another timezone, or a page left open past
    midnight, would otherwise file this morning's work against yesterday. Refused rather than
    silently redirected — "reload" is the honest answer, not "I moved your ticks".

    `packed` defaults to TRUE when absent, which is what makes this scanner-ready: a barcode
    reader can post `{"amazon_order_id": "..."}` alone and mean "this box is done". `source`
    records how the tick arrived ("manual" or "scan") so the two can be told apart later.

    Writes no order status and reaches no invoice. Amazon has no opinion about whether a box is
    finished on this floor, so this is not a second source of truth — the same boundary every
    other write in this feature respects.
    """
    today = datetime.now(logic.IST).date().isoformat()
    if pack_date != today:
        return JSONResponse(
            {
                "error": f"That page is for {pack_date}, but today is {today} (IST). "
                         "Reload the page before ticking more orders.",
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
            {"error": "entries must be a list of {amazon_order_id, packed} objects."},
            status_code=400,
        )

    order_packed = await repository.save_order_packed(
        db, pack_date, entries, packed_by=getattr(grant, "username", "") or ""
    )
    # The whole map goes back, not just what was sent: the screen re-renders from committed
    # truth rather than from what it believes it saved, because a warehouse phone can lose a
    # response and a stale tick is what gets acted on.
    logger.info(
        "orders: %d order tick(s) saved for %s (%d packed)",
        len(entries), pack_date, len(order_packed),
    )
    return JSONResponse(
        {"status": "saved", "pack_date": pack_date, "order_packed": order_packed}
    )


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
    tab: str = "all",
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """The dispatch sheet as a PDF — all sections, or one of them.

    Built through the same `_dispatch` the screen uses, so the paper and the monitor cannot
    disagree. A PDF because this one is read on the floor and ticked with a pen; the Excel
    variants are for accounts and for suppliers.
    """
    if tab not in DOWNLOAD_TABS:
        return JSONResponse({"error": f"Unknown section {tab!r}."}, status_code=400)

    sheet, purchasing, meta = await _dispatch(db)
    stream = documents.build_dispatch_pdf(
        sheet, _dispatch_subtitle(sheet, meta), tab=tab, purchasing=purchasing
    )
    filename = f"dispatch-{tab}-{meta['today']}.pdf"
    return StreamingResponse(
        stream,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/download/dispatch.xlsx")
async def download_dispatch_xlsx(
    request: Request,
    tab: str = "all",
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """The dispatch sheet as Excel: the combined workbook, one tab, or the to-buy list.

    `tab=tobuy` holds only the products that are short, for pasting into a supplier email.
    """
    if tab not in DOWNLOAD_TABS + XLSX_ONLY_TABS:
        return JSONResponse({"error": f"Unknown section {tab!r}."}, status_code=400)

    sheet, purchasing, meta = await _dispatch(db)
    subtitle = _dispatch_subtitle(sheet, meta)

    if tab == "tobuy":
        stream = documents.build_tobuy_xlsx(purchasing, subtitle)
        filename = f"to-buy-{meta['today']}.xlsx"
    else:
        stream = documents.build_dispatch_xlsx(sheet, purchasing, subtitle, tab=tab)
        filename = f"dispatch-{tab}-{meta['today']}.xlsx"

    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
