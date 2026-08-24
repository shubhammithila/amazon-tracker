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

#: Both statuses that still need packing. PartiallyShipped is included because a partly
#: shipped order still has units on the floor.
OPEN_STATUSES = "Unshipped,PartiallyShipped"


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


async def fetch_easy_ship_orders(
    days: int = 90,
    *,
    max_pages: int = 10,
    sleep=asyncio.sleep,
) -> tuple[list[dict], list[str]]:
    """Every Easy Ship order created in the last `days`, paged. Returns (orders, warnings).

    **Waits `ORDERS_MIN_INTERVAL` between pages.** Not politeness: Amazon allows one call
    every 22.2 seconds and a 429 costs the page. `sleep` is injectable so tests assert the
    delay without spending it.

    **Filters to Easy Ship on the SERVICE LEVEL.** `FulfillmentChannel` reads MFN for both
    Easy Ship and plain self-ship, and the non-Easy-Ship orders carry a 1995 ship-by
    sentinel that would sit at the top of the packer's sheet as 31 years overdue.

    **`max_pages` is a cap, and reaching it is REPORTED.** Amazon keeps issuing NextToken
    while more exists; unbounded, a first run could page for as long as the tokens last.
    A silent truncation would have the owner believe he had seen every order.
    """
    settings = get_settings()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params: dict = {
        "MarketplaceIds": settings.sp_api_marketplace_id,
        "CreatedAfter": since,
        "OrderStatuses": OPEN_STATUSES,
    }
    orders: list[dict] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    token: str | None = None
    page = 0

    for page in range(max_pages):
        if page:
            # The wait belongs BEFORE the call, not after the last one: sleeping after the
            # final page would add 22 idle seconds to every refresh.
            await sleep(ORDERS_MIN_INTERVAL)
            params = {
                "MarketplaceIds": settings.sp_api_marketplace_id,
                "NextToken": token,
            }

        payload = (await spapi._get("/orders/v0/orders", params=params)).get("payload") or {}
        raw = payload.get("Orders") or []
        for item in raw:
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
    else:
        if token:
            warnings.append(
                f"Stopped after {max_pages} pages and Amazon reports more pages of orders. "
                "Older orders were not fetched — run the refresh again to continue."
            )

    logger.info("orders: fetched %d Easy Ship order(s) in %d page(s)", len(orders), page + 1)
    return orders, warnings


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
