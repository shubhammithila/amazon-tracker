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
from datetime import datetime, time, timedelta, timezone

from app.config import get_settings
from app.orders.logic import IST, is_easy_ship
from app.shipment import spapi
from app.shipment.spapi import SpApiError

logger = logging.getLogger(__name__)

#: Seconds between getOrders calls. Amazon reports 0.04512 req/sec = one per 22.2s;
#: rounded UP to 22.5 because undershooting earns a 429 that costs the whole page.
ORDERS_MIN_INTERVAL = 22.5

#: Seconds between getOrderItems calls. Amazon returns `x-amzn-RateLimit-Limit: 0.5` —
#: measured on the live account — which is one call every 2.0 seconds exactly.
#:
#: **2.2, not 2.0, and the margin is the whole point.** This was 2.0, sitting precisely ON
#: the limit with nothing spare, under a comment guessing that "3-4 new orders a day" made
#: the interval academic. The real backlog is 235 orders needing items, and a 200-call run at
#: dead-on 2.0s earned `You exceeded your quota for the requested resource` on 54 of them:
#: any jitter or retry makes the effective rate faster than the bucket refills. Same
#: undershoot mistake `ORDERS_MIN_INTERVAL` already documents — 22.2 measured, 22.5 used.
#:
#: A dropped item call is not cosmetic: the order still appears in its section while
#: contributing no units, so the sheet silently understates what has to be picked.
ITEMS_MIN_INTERVAL = 2.2

#: Consecutive quota errors before the item phase gives up for this run. Once the bucket is
#: empty every further call fails identically, so continuing spends requests against an
#: account Amazon is already throttling. 5 distinguishes "the bucket is empty" from one
#: cancelled order returning 404. The skipped orders keep `items_fetched_at` NULL and are
#: retried next run.
ITEMS_THROTTLE_GIVE_UP = 5

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

#: **Every status a dispatched order can be in, including the collected ones.**
#:
#: `getOrders` pages OLDEST-FIRST with no sort parameter, so the filter is what decides whether
#: today's orders are reachable at all. Two corrections are recorded here, in order.
#:
#: **First:** filtering on ORDER status alone returned 100 `Delivered` orders per page — 165
#: across 6 pages, with every section of the sheet reading zero while Seller Central showed
#: 371 waiting. Filtering on `EasyShipShipmentStatuses` fixed that.
#:
#: **Second, and the reason `PickedUp` is in this list:** `PendingSchedule,PendingPickUp` was
#: not enough either. Amazon collects within hours, so an order labelled AND collected between
#: two refreshes was never in the answer — and the reconcile pass could not rescue it, because
#: that only re-reads orders already held. Measured on 2026-08-26: `PendingPickUp` returned
#: **0**, every one of the day's orders was `PickedUp`, and the screen showed 95 where Seller
#: Central showed 194.
#:
#: A collected order is still today's dispatch — the boxes were packed on this floor this
#: morning — which is exactly what `logic.is_todays_dispatch` already encodes. The fetch has to
#: agree with the predicate, or the predicate never sees the rows.
#:
#: `LABELLED_EASYSHIP` in `orders.logic` is the same idea for a row we already hold; this is
#: the wire format Amazon wants. They are deliberately separate: this one omits the
#: never-observed doc aliases, because sending Amazon a status it does not recognise risks the
#: whole query rather than merely matching nothing.
#: `ReturningToSeller` / `ReturnedToSeller` are included for the same reason as `PickedUp`: the
#: parcel went out today, so it was packed today, and the day's tally should not shrink because
#: a customer refused it hours later. None are currently held with a recent ship-by, so this
#: costs nothing today and closes the gap before it happens.
ACTIONABLE_EASYSHIP_STATUSES = (
    "PendingSchedule,PendingPickUp,PickedUp,OutForDelivery,Delivered,"
    "ReturningToSeller,ReturnedToSeller"
)

#: The same statuses as a set: which LOCAL rows the fetch was authoritative about.
#:
#: Derived from the string rather than written twice, because the reconcile pass treats "we hold
#: this status but Amazon did not return it" as "this order changed" — if the two drifted apart
#: it would either re-read orders pointlessly or leave stale rows uncorrected.
FETCHED_STATUS_SET = frozenset(
    part.strip() for part in ACTIONABLE_EASYSHIP_STATUSES.split(",") if part.strip()
)

#: Orders that still need something from the warehouse, and therefore deserve the item budget
#: first.
#:
#: **Deliberately NARROWER than `FETCHED_STATUS_SET`, and the split is a bug fix.** One set used
#: to serve both jobs. Adding the collected statuses to the fetch — which is what made today's
#: 194 orders visible — silently promoted 359 `Delivered` orders to the front of the item queue,
#: so `getOrderItems` would have spent its 200-call cap on parcels already with the customer
#: while the orders still to pack got nothing. The sheet counts ITEMS, so those orders would
#: have appeared with no units: the exact "168 units across 265 orders" failure that the
#: priority ordering was added to fix in the first place.
#:
#: A delivered order is still FETCHED (its row must stay current) but is not PRIORITISED.
NEEDS_WORK_STATUS_SET = frozenset({"PendingSchedule", "PendingPickUp"})

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

#: `days=TODAY_ONLY` means "since midnight IST" rather than a count of days back.
#:
#: **This is what makes the routine refresh both correct and fast**, and it is the opposite of
#: the tuning that preceded it. With `PickedUp` now in the filter, a wide window is no longer
#: free: that status has months of history, and `getOrders` pages oldest-first, so a 90-day
#: window spent 8 pages (~3 minutes) on ancient orders and never reached today. Measured on
#: 2026-08-26: 800 rows fetched, **0** of them due today.
#:
#: A window starting at midnight IST puts today's orders on page 1 — 193 of them, matching
#: Seller Central — because an order dispatched today was necessarily updated today.
#:
#: A sentinel rather than a fraction of a day, because "today" is a CALENDAR question in IST
#: and `days=0.5` would drift with the hour the job happens to run.
TODAY_ONLY = "today"


def _since(days) -> str:
    """The `LastUpdatedAfter` value for a window, as Amazon's ISO-8601.

    `TODAY_ONLY` resolves to midnight IST expressed in UTC — the start of the business day, not
    of the UTC day. Those differ by 5.5 hours, and using the UTC day boundary would drop every
    order placed between 00:00 and 05:30 IST from the routine refresh.
    """
    if days == TODAY_ONLY:
        start = datetime.combine(
            datetime.now(IST).date(), time.min, tzinfo=IST
        )
        return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        datetime.now(timezone.utc) - timedelta(days=float(days))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    on_page=None,
) -> tuple[list[dict], bool]:
    """Page one `getOrders` query to exhaustion or `max_pages`. Returns (orders, truncated).

    Extracted so all three passes share ONE rate-limit and de-duplication implementation.
    Three copies of a 22.5-second sleep is three chances for one of them to be missing, and
    the symptom of a missing wait is a 429 that costs the whole page.

    `seen_ids` is shared ACROSS passes by the caller: the pending pass and the actionable
    pass can legitimately return the same order if its payment confirms mid-refresh, and the
    first answer is the one already paid for.

    `on_page(pages_done, max_pages, orders_so_far)` is called after each page, so the caller
    can publish progress while a multi-minute pass is still running. Optional, because the
    parsing tests have no interest in it.
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

        if on_page is not None:
            # Reported per page rather than at the end: a page costs 22.5 seconds, so a
            # progress bar that only moved when the pass finished would sit still for
            # minutes — which is what the owner reads as "it has hung".
            on_page(pages, max_pages, len(orders))

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
    on_page=None,
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
    since = _since(days)

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
        on_page=on_page,
    )
    orders.extend(actionable)
    if truncated:
        # **This message must not claim today's sheet is incomplete — it said so, and it was
        # false.** `getOrders` pages OLDEST-FIRST, and the window asks for everything Amazon
        # UPDATED today, which includes last week's orders as they walk to `Delivered`. So the
        # rows lost to the cap are the OLDEST ones, while today's dispatch — necessarily the
        # most recently updated — is on the pages that were fetched.
        #
        # The old wording sent the warehouse looking for missing parcels that were on the
        # screen all along, every half hour. What truncation actually costs is the reconcile
        # signal for older orders: a status change on a parcel from last week may be a refresh
        # late. That is worth stating, and it is not the same claim.
        warnings.append(
            f"Amazon had more than {max_pages} pages of updated orders, so the oldest ones "
            "were not re-read this time. Today's dispatch is complete — Amazon returns the "
            "oldest first, so only older orders' statuses may lag by a refresh."
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
        on_page=on_page,
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
    order_ids: Sequence[str], *, sleep=asyncio.sleep, on_item=None
) -> dict[str, list[dict]]:
    """Line items for the named orders. `{order_id: [item, ...]}`.

    The CALLER decides which orders need items — it passes only those whose
    `items_fetched_at` is NULL — so re-refreshing 100 known orders costs zero calls here.

    **One order failing does not abandon the batch.** An order can be cancelled between
    the list call and this one, and losing every other order's items to a single 404 would
    waste minutes of rate-limited work. The failure is logged and that order is simply
    absent from the result, so the caller leaves its `items_fetched_at` NULL and retries
    next time.

    **A run of quota errors STOPS the phase instead of grinding through it.** Measured: once
    the bucket is empty every remaining call fails the same way, and a 200-order batch spent
    54 consecutive failures learning nothing — two minutes of wall clock and 54 requests
    against an account Amazon is already throttling. Those orders keep `items_fetched_at`
    NULL, so the next run picks them up; giving up early is what makes the next run possible
    sooner.

    A one-off failure is different and does NOT stop anything: an order can be cancelled
    between the list call and this one, and a single 404 must not cost the batch.
    """
    out: dict[str, list[dict]] = {}
    consecutive_throttles = 0
    total = len(order_ids)
    for index, order_id in enumerate(order_ids):
        if index:
            await sleep(ITEMS_MIN_INTERVAL)
        try:
            payload = await spapi._get(f"/orders/v0/orders/{order_id}/orderItems")
        except SpApiError as exc:            # noqa: BLE001 - logged, and retried next run
            logger.warning("orders: items for %s failed (%s)", order_id, exc)
            if getattr(exc, "status", None) == 429 or "quota" in str(exc).lower():
                consecutive_throttles += 1
                if consecutive_throttles >= ITEMS_THROTTLE_GIVE_UP:
                    logger.warning(
                        "orders: giving up on items after %d consecutive quota errors; "
                        "%d order(s) keep items_fetched_at NULL and retry next run",
                        consecutive_throttles, len(order_ids) - index - 1,
                    )
                    break
            # Reported even on failure, so the bar keeps moving through a run of 404s
            # instead of freezing on a number that will never be reached.
            if on_item is not None:
                on_item(index + 1, total)
            continue
        consecutive_throttles = 0
        out[order_id] = parse_items(payload)
        if on_item is not None:
            on_item(index + 1, total)
    return out
