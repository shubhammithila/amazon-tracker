"""The only caller of Amazon's Orders API.

Auth, error typing and connection reuse come from ``app.shipment.spapi`` rather than being
re-implemented: one token cache and one error type across the app means an auth failure
reads the same wherever it happens.

**Everything here is rate-limited by one number.** Amazon returned
``x-amzn-RateLimit-Limit: 0.04512`` for ``getOrders`` — one request every 22.2 seconds. A
90-day window pages, so a full fetch is minutes of wall-clock time. That single fact is
why orders are cached in the database and why this module is never called from a request
handler.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.orders.logic import is_easy_ship
from app.shipment import spapi
from app.shipment.spapi import SpApiError

logger = logging.getLogger(__name__)

#: Seconds between getOrders calls. Amazon reports 0.04512 req/sec = one per 22.2s;
#: rounded UP to 22.5 because undershooting earns a 429 that costs the whole page.
ORDERS_MIN_INTERVAL = 22.5

#: getOrderItems is documented at ~0.5 req/sec and measured far cheaper than getOrders,
#: but a burst of 100 would still trip it. 2s is comfortable for the 3-4 new orders a day
#: this account actually sees.
ITEMS_MIN_INTERVAL = 2.0

#: Every status the Orders tab needs, and `Shipped` is the one that matters most.
#:
#: **`Shipped` was missing, and that made 247 orders invisible.** In Amazon's model an order
#: becomes `Shipped` the moment a label is generated — so "waiting for pickup", the boxes
#: physically standing in the warehouse, are `Shipped` with `EasyShipShipmentStatus:
#: PendingPickUp`. Fetching only Unshipped meant the awaiting-pickup section could never
#: populate, while Seller Central showed 247 of them.
#:
#: `Pending` is payment-not-yet-confirmed: it cannot be packed, but it is what is coming, so
#: it gets its own section rather than being hidden.
#:
#: `Canceled` is deliberately absent. It is the only status with nothing to show and no
#: transition out, and including it would spend item calls on parcels that will never exist.
OPEN_STATUSES = "Pending,Unshipped,PartiallyShipped,Shipped"

#: **The filter that makes this fetch correct rather than merely cheaper.**
#:
#: Order-status filtering alone was not enough. `getOrders` pages OLDEST-FIRST and offers no
#: sort parameter, so a 14-day window over open statuses returned 100 orders per page of
#: which every single one was `Delivered` — 165 orders across 6 pages, and every section of
#: the picking sheet read zero while Seller Central showed 371 waiting. Paging far enough to
#: reach today would have cost minutes per refresh at 22.5 seconds a page, and would have
#: got worse with every order the business shipped.
#:
#: Narrowing the WINDOW cannot fix it either: 100+ orders change status every day, mostly to
#: `Delivered`, so any window wide enough to be safe is also wide enough to fill with noise.
#:
#: Measured with this filter: the same query returns 97 `PendingSchedule` + 3 `PendingPickUp`
#: on page 1, and the complete actionable set is **371 orders in 4 pages** — bounded by the
#: FILTER, not by the window. That is why the window can now be wide and the page cap low.
ACTIONABLE_EASYSHIP_STATUSES = "PendingSchedule,PendingPickUp"

#: The same statuses as a set, for deciding which LOCAL rows the fetch was authoritative
#: about. Derived from the string rather than written twice: the reconcile pass treats "we
#: hold this status but Amazon did not return it" as "this order changed", so if the two
#: drifted apart it would either re-read orders pointlessly or leave stale rows uncorrected.
ACTIONABLE_STATUS_SET = frozenset(
    part.strip() for part in ACTIONABLE_EASYSHIP_STATUSES.split(",") if part.strip()
)

#: Easy Ship and plain self-ship are both `MFN`; FBA is `AFN`. Filtering to MFN drops every
#: FBA order at Amazon's end rather than fetching it and discarding it here.
#:
#: This is a rate-budget fix, not a correctness one — `is_easy_ship` already rejected them —
#: but it was worth 94 of the 100 orders on the `Pending` page: unfiltered, that page held
#: 100 FBA `Expedited` orders and not one Easy Ship order.
MFN_CHANNEL = "MFN"

#: Payment-unconfirmed orders carry **no `EasyShipShipmentStatus` at all**, so
#: `ACTIONABLE_EASYSHIP_STATUSES` excludes them by construction and they need their own pass.
#: Measured: 6 Easy Ship `Pending` orders, one page, all `EasyShipShipmentStatus: None`.
PENDING_ORDER_STATUS = "Pending"

#: Pages for the pending pass. Six orders fit in one page many times over; 2 is headroom.
PENDING_MAX_PAGES = 2

#: Ids per `AmazonOrderIds` re-read. Amazon's documented ceiling is 50.
ORDER_ID_BATCH = 50

#: Orders re-read per refresh to resolve dropped-out rows. 4 calls at 22.5s; the remainder
#: waits for the next run, which is correct because a stale row is wrong but not dangerous.
RECONCILE_LIMIT = 200


def _dt(value) -> datetime | None:
    """An Amazon ISO-8601 timestamp as a NAIVE UTC datetime.

    Naive to match the `*_utc` columns, which SQLAlchemy stores without a timezone. The
    conversion to IST happens once, at render time, in ``orders.logic.to_ist`` — storing
    IST here would put local time in a column named `_utc` and mislead every later reader.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("orders: could not parse timestamp %r", value)
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _money(block) -> float | None:
    """The amount out of an Amazon money block, or None.

    None rather than 0.0: a cancelled order genuinely has no total, and 0.0 would render
    as a real 0 order on the reconciliation list.
    """
    if not isinstance(block, dict):
        return None
    try:
        return float(block.get("Amount"))
    except (TypeError, ValueError):
        return None


def parse_order(payload: dict) -> dict:
    """One API order as the `amazon_orders` column shape.

    The ONLY place Amazon's field names are translated to ours, so a renamed field fails
    in one test rather than surfacing as a NULL column on the picking sheet.

    **Every field is optional.** A cancelled order arrives without `OrderTotal` or
    `ShippingAddress`, and dying on one odd order would lose the whole page — which, at 22
    seconds a page, is minutes of work.
    """
    address = payload.get("ShippingAddress") or {}
    level = payload.get("ShipServiceLevel") or ""
    return {
        "amazon_order_id": payload.get("AmazonOrderId") or "",
        "purchase_date_utc": _dt(payload.get("PurchaseDate")),
        "latest_ship_date_utc": _dt(payload.get("LatestShipDate")),
        "status": payload.get("OrderStatus"),
        "easyship_status": payload.get("EasyShipShipmentStatus"),
        "ship_service_level": level,
        "order_total": _money(payload.get("OrderTotal")),
        "currency": (payload.get("OrderTotal") or {}).get("CurrencyCode"),
        "items_ordered": int(payload.get("NumberOfItemsUnshipped") or 0)
                         + int(payload.get("NumberOfItemsShipped") or 0),
        "items_shipped": int(payload.get("NumberOfItemsShipped") or 0),
        "is_prime": bool(payload.get("IsPrime")),
        # Read off the service level, not PaymentMethod: measured, PaymentMethod reads
        # "Other" on real COD orders while the level says "Std IN EZ National COD".
        "is_cod": "COD" in level.upper(),
        "city": address.get("City"),
        "state": address.get("StateOrRegion"),
        "postal_code": address.get("PostalCode"),
    }


def parse_items(payload: dict) -> list[dict]:
    """The `amazon_order_items` rows out of a getOrderItems response.

    A zero-quantity line is dropped: it contributes nothing to a pick and would add a
    0-unit row to the picking sheet.
    """
    rows = []
    for item in (payload.get("payload") or {}).get("OrderItems") or []:
        asin = (item.get("ASIN") or "").strip().upper()
        quantity = int(item.get("QuantityOrdered") or 0)
        if not asin or quantity <= 0:
            continue
        rows.append({
            "asin": asin,
            "seller_sku": item.get("SellerSKU"),
            "title": item.get("Title"),
            "quantity_ordered": quantity,
            "quantity_shipped": int(item.get("QuantityShipped") or 0),
            "item_price": _money(item.get("ItemPrice")),
            "item_tax": _money(item.get("ItemTax")),
            "promotion_discount": _money(item.get("PromotionDiscount")),
        })
    return rows


async def _page_orders(
    params: dict,
    *,
    max_pages: int,
    sleep,
    seen_ids: set[str],
    label: str,
) -> tuple[list[dict], bool]:
    """Page one `getOrders` query to exhaustion or `max_pages`. Returns (orders, truncated).

    Extracted so all three passes share ONE rate-limit and de-duplication implementation.
    Three copies of a 22.5-second sleep is three chances for one of them to be missing, and
    the symptom of a missing wait is a 429 that costs the whole page.

    `seen_ids` is shared ACROSS passes by the caller: the pending pass and the actionable
    pass can legitimately return the same order if its payment confirms mid-refresh, and the
    first answer is the one already paid for.
    """
    settings = get_settings()
    orders: list[dict] = []
    token: str | None = None
    pages = 0

    for index in range(max_pages):
        if index:
            # The wait belongs BEFORE the call, not after the last one: sleeping after the
            # final page would add 22 idle seconds to every refresh.
            await sleep(ORDERS_MIN_INTERVAL)
            params = {
                "MarketplaceIds": settings.sp_api_marketplace_id,
                "NextToken": token,
            }

        payload = (await spapi._get("/orders/v0/orders", params=params)).get("payload") or {}
        pages = index + 1
        for item in payload.get("Orders") or []:
            row = parse_order(item)
            if not row["amazon_order_id"] or row["amazon_order_id"] in seen_ids:
                continue
            if not is_easy_ship(row["ship_service_level"]):
                continue
            seen_ids.add(row["amazon_order_id"])
            orders.append(row)

        token = payload.get("NextToken")
        if not token:
            break

    logger.info("orders: %s returned %d order(s) in %d page(s)", label, len(orders), pages)
    return orders, bool(token)


async def fetch_easy_ship_orders(
    days: int = 90,
    *,
    max_pages: int = 10,
    sleep=asyncio.sleep,
) -> tuple[list[dict], list[str]]:
    """Every Easy Ship order that needs the warehouse's attention. Returns (orders, warnings).

    **Two passes, because one query cannot express the question.** The actionable pass asks
    for the orders with work outstanding; the pending pass asks for payment-unconfirmed
    orders, which carry no `EasyShipShipmentStatus` and are therefore invisible to the first
    filter. Both are needed and neither subsumes the other.

    **The status filter is what makes this correct, not merely fast.** `getOrders` pages
    OLDEST-FIRST with no sort parameter, so the previous version — open order statuses over a
    14-day window — spent 6 pages and returned 165 orders that were every one `Delivered`,
    while 371 orders sat waiting in Seller Central and every section of the sheet read zero.
    Filtering on `EasyShipShipmentStatuses` bounds the result by RELEVANCE instead of by
    date: measured, the complete actionable set is 371 orders in 4 pages.

    That is also why `days` is now a wide backstop rather than a tuning knob. The filter
    bounds the result, so a wide window costs nothing and protects against an old order that
    is still genuinely unshipped.

    **Waits `ORDERS_MIN_INTERVAL` between pages.** Amazon allows one call every 22.2 seconds
    and a 429 costs the page. `sleep` is injectable so tests assert the delay without
    spending it.

    **Reaching `max_pages` is REPORTED, per pass.** A silent truncation would have the owner
    believe he had seen every order — which is the exact failure this function was rewritten
    to fix, so it must not be reintroduced by the fix.
    """
    settings = get_settings()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # `LastUpdatedAfter`, NOT `CreatedAfter`. What this screen cares about is a change of
    # STATE, not a new order: an order placed three weeks ago that was labelled this morning
    # must appear in "waiting for pickup" today. Keyed on creation, it would fall out of a
    # routine window and silently stop being tracked while its parcel sat on the floor.
    orders: list[dict] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    actionable, truncated = await _page_orders(
        {
            "MarketplaceIds": settings.sp_api_marketplace_id,
            "LastUpdatedAfter": since,
            "FulfillmentChannels": MFN_CHANNEL,
            "EasyShipShipmentStatuses": ACTIONABLE_EASYSHIP_STATUSES,
        },
        max_pages=max_pages,
        sleep=sleep,
        seen_ids=seen_ids,
        label="actionable",
    )
    orders.extend(actionable)
    if truncated:
        warnings.append(
            f"Stopped after {max_pages} pages of orders still to pack or hand over, and "
            "Amazon reports more. Some orders are missing from today's sheet — run the "
            "refresh again to continue."
        )

    # The pending pass. Separate because a payment-unconfirmed order has no Easy Ship status
    # at all, so no value in ACTIONABLE_EASYSHIP_STATUSES can ever match it.
    await sleep(ORDERS_MIN_INTERVAL)
    pending, pending_truncated = await _page_orders(
        {
            "MarketplaceIds": settings.sp_api_marketplace_id,
            "LastUpdatedAfter": since,
            "FulfillmentChannels": MFN_CHANNEL,
            "OrderStatuses": PENDING_ORDER_STATUS,
        },
        max_pages=PENDING_MAX_PAGES,
        sleep=sleep,
        seen_ids=seen_ids,
        label="pending payment",
    )
    orders.extend(pending)
    if pending_truncated:
        warnings.append(
            "Amazon reports more pending-payment orders than were fetched. They cannot be "
            "packed yet, so today's sheet is unaffected."
        )

    logger.info("orders: %d actionable + %d pending", len(actionable), len(pending))
    return orders, warnings


async def fetch_orders_by_id(
    order_ids: Sequence[str], *, sleep=asyncio.sleep
) -> list[dict]:
    """Re-read specific orders by id, whatever their status. Verified against the account.

    **This is what stops a stale row lying.** The actionable query returns only orders with
    work outstanding, so an order that gets picked up DISAPPEARS from it — and a local row
    left untouched would keep saying "waiting for pickup" for ever, sending someone to look
    for a parcel the courier already took.

    The honest fix is to ask Amazon what those orders are now, rather than inferring a status
    from their absence. `AmazonOrderIds` accepts up to `ORDER_ID_BATCH` ids and needs no date
    filter, so reconciling 200 orders costs 4 calls instead of re-paging the world.

    Easy Ship filtering is deliberately NOT applied: the caller is asking about orders it
    already holds, and dropping a row here would leave the very staleness this exists to
    remove.
    """
    out: list[dict] = []
    batches = [
        list(order_ids)[start:start + ORDER_ID_BATCH]
        for start in range(0, len(order_ids), ORDER_ID_BATCH)
    ]
    settings = get_settings()
    for index, batch in enumerate(batches):
        if index:
            await sleep(ORDERS_MIN_INTERVAL)
        try:
            payload = (await spapi._get("/orders/v0/orders", params={
                "MarketplaceIds": settings.sp_api_marketplace_id,
                "AmazonOrderIds": ",".join(batch),
            })).get("payload") or {}
        except SpApiError as exc:       # noqa: BLE001 - logged; the rows simply stay as they are
            # One failed batch must not abandon the rest: these are corrections to rows that
            # already exist, so a partial reconcile is strictly better than none.
            logger.warning("orders: re-read of %d id(s) failed (%s)", len(batch), exc)
            continue
        for item in payload.get("Orders") or []:
            row = parse_order(item)
            if row["amazon_order_id"]:
                out.append(row)
    logger.info("orders: re-read %d of %d requested", len(out), len(order_ids))
    return out


async def fetch_items(
    order_ids: Sequence[str], *, sleep=asyncio.sleep
) -> dict[str, list[dict]]:
    """Line items for the named orders. `{order_id: [item, ...]}`.

    The CALLER decides which orders need items — it passes only those whose
    `items_fetched_at` is NULL — so re-refreshing 100 known orders costs zero calls here.

    **One order failing does not abandon the batch.** An order can be cancelled between
    the list call and this one, and losing every other order's items to a single 404 would
    waste minutes of rate-limited work. The failure is logged and that order is simply
    absent from the result, so the caller leaves its `items_fetched_at` NULL and retries
    next time.
    """
    out: dict[str, list[dict]] = {}
    for index, order_id in enumerate(order_ids):
        if index:
            await sleep(ITEMS_MIN_INTERVAL)
        try:
            payload = await spapi._get(f"/orders/v0/orders/{order_id}/orderItems")
        except SpApiError as exc:            # noqa: BLE001 - logged, and retried next run
            logger.warning("orders: items for %s failed (%s)", order_id, exc)
            continue
        out[order_id] = parse_items(payload)
    return out
