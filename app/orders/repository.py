"""The only reader and writer of Amazon order rows.

Written as SELECT-then-UPDATE-or-INSERT rather than a dialect-specific upsert, so the same
code runs on SQLite locally and PostgreSQL in production — the same reasoning
``app/shipment/repository.py`` documents.

**These rows are a cache of Amazon's data.** Nothing outside the refresh job writes them,
and no screen edits them: if a value looks wrong the fix is a refresh. A local edit would
create a second source of truth about whether an order shipped, which is the class of bug
the shipment feature's write separation exists to avoid.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import case, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AmazonOrder,
    AmazonOrderItem,
    OrderPackedEntry,
    OrderPackedState,
    ProductRawStock,
)

logger = logging.getLogger(__name__)

#: Columns the refresh may write. `first_seen_at` is deliberately absent — see
#: `upsert_orders`.
WRITABLE = (
    "purchase_date_utc", "latest_ship_date_utc", "status", "easyship_status",
    "ship_service_level", "order_total", "currency", "items_ordered", "items_shipped",
    "is_prime", "is_cod", "city", "state", "postal_code",
)


async def upsert_orders(db: AsyncSession, rows: list[dict]) -> tuple[int, int]:
    """Store orders, updating those already known. Returns ``(created, updated)``.

    A refresh runs daily and on demand, so the same order arrives many times. Inserting
    each arrival would multiply every quantity on the picking sheet by the number of
    refreshes — the same double-count the ``(plan_id, pack_date)`` index prevents for
    packing days.

    **`first_seen_at` is written once and never updated.** "New since I last looked" and
    "Amazon changed something" are different questions, and overwriting it would make every
    order look new every morning.

    **`items_fetched_at` is left alone.** An update must not un-fetch the line items, or a
    daily refresh would re-fetch every order's items for ever.
    """
    if not rows:
        return 0, 0

    ids = [r.get("amazon_order_id") for r in rows if r.get("amazon_order_id")]
    existing = {
        row.amazon_order_id: row
        for row in (
            await db.execute(
                select(AmazonOrder).where(AmazonOrder.amazon_order_id.in_(ids))
            )
        ).scalars()
    }

    now = datetime.utcnow()
    created = updated = 0
    for payload in rows:
        order_id = payload.get("amazon_order_id")
        if not order_id:
            continue
        row = existing.get(order_id)
        if row is None:
            row = AmazonOrder(amazon_order_id=order_id, first_seen_at=now)
            db.add(row)
            created += 1
        else:
            updated += 1
        for field in WRITABLE:
            if field in payload:
                setattr(row, field, payload[field])
        row.last_refreshed_at = now

    await db.commit()
    return created, updated


async def replace_items(db: AsyncSession, amazon_order_id: str, items: list[dict]) -> int:
    """Replace one order's line items wholesale. Returns how many were stored.

    **Replace, never append.** Amazon's list is the truth, and appending would double a
    quantity on the picking sheet every time the items were re-fetched — quantity being
    exactly what the warehouse picks against.

    **`items_fetched_at` is stamped even when the list is EMPTY.** A cancelled order can
    legitimately return zero lines; leaving the column NULL would queue it again on every
    refresh, spending a call each time and never succeeding differently.
    """
    order = (
        await db.execute(
            select(AmazonOrder).where(AmazonOrder.amazon_order_id == amazon_order_id)
        )
    ).scalar_one_or_none()
    if order is None:
        logger.warning("orders: items for unknown order %s discarded", amazon_order_id)
        return 0

    await db.execute(
        delete(AmazonOrderItem).where(AmazonOrderItem.order_id == order.id)
    )

    stored = 0
    for item in items or []:
        asin = (item.get("asin") or "").strip().upper()
        if not asin:
            continue
        db.add(AmazonOrderItem(
            order_id=order.id,
            asin=asin,
            seller_sku=item.get("seller_sku"),
            title=item.get("title"),
            quantity_ordered=int(item.get("quantity_ordered") or 0),
            quantity_shipped=int(item.get("quantity_shipped") or 0),
            item_price=item.get("item_price"),
            item_tax=item.get("item_tax"),
            promotion_discount=item.get("promotion_discount"),
        ))
        stored += 1

    order.items_fetched_at = datetime.utcnow()
    await db.commit()
    return stored


async def ids_missing_items(
    db: AsyncSession, limit: int = 200, *, priority_statuses: set[str] | None = None
) -> list[str]:
    """Order ids whose line items have never been fetched. Actionable orders FIRST.

    This is what keeps a daily refresh cheap: `getOrderItems` costs a call per order, so
    re-fetching 100 known orders would spend 100 calls to learn nothing — over three
    minutes at the interval the client uses.

    Capped, because a first run against 90 days of history would otherwise queue hundreds
    of calls into one job. The remainder is picked up by the next refresh.

    **`priority_statuses` decides who wins that cap, and it decides whether the sheet adds
    up.** Items are what the picking sheet counts, and ordering by purchase date alone spent
    the budget on whatever was newest — measured on the live account, 165 of 378 orders were
    `Delivered` or `ReturnedToSeller`, which appear in no section at all. They crowded out
    orders awaiting pickup, so the sheet reported 168 units across 265 orders: fewer units
    than orders, for products nobody buys in fractions.

    An order with no items is INVISIBLE on the sheet even when its own section counts it, so
    this ordering is the difference between a sheet the warehouse can pick against and one
    that silently understates the work.
    """
    query = select(AmazonOrder.amazon_order_id).where(AmazonOrder.items_fetched_at.is_(None))

    if priority_statuses:
        # A CASE expression rather than two queries: one round trip, and the cap then applies
        # to the merged ordering instead of being split between them by hand.
        actionable_first = case(
            (AmazonOrder.easyship_status.in_(sorted(priority_statuses)), 0), else_=1
        )
        query = query.order_by(actionable_first, AmazonOrder.purchase_date_utc.desc())
    else:
        query = query.order_by(AmazonOrder.purchase_date_utc.desc())

    rows = await db.execute(query.limit(limit))
    return list(rows.scalars())


async def ids_needing_reconcile(
    db: AsyncSession, fetched_ids: set[str], statuses: set[str], limit: int = 200
) -> list[str]:
    """Locally-actionable orders that a complete fetch did NOT return, oldest first.

    **These are the rows that would otherwise lie.** The refresh asks Amazon only for orders
    with work outstanding, so an order that has been picked up drops out of the answer. Its
    local row still reads `PendingPickUp`, and nothing in a plain upsert would ever correct
    it — the packer would keep being told to hand over a parcel the courier already took.

    Absence is only meaningful because that fetch is COMPLETE: the actionable query exhausts
    its `NextToken` in 4 pages, so an order we hold as actionable and Amazon did not return
    has genuinely changed. The caller re-reads these by id rather than assuming what they
    became — inferring "probably picked up" would be inventing Amazon's data, which is the
    one thing this cache must never do.

    `fetched_ids` empty means the fetch returned nothing at all — a failure, not an empty
    warehouse — so nothing is reconciled. Treating a failed fetch as "everything changed"
    would spend the whole rate budget re-reading rows that were already right.
    """
    if not fetched_ids or not statuses:
        return []

    rows = await db.execute(
        select(AmazonOrder.amazon_order_id)
        .where(
            AmazonOrder.easyship_status.in_(sorted(statuses)),
            AmazonOrder.amazon_order_id.notin_(sorted(fetched_ids)),
        )
        .order_by(AmazonOrder.last_refreshed_at.asc())
        .limit(limit)
    )
    return list(rows.scalars())


async def load_orders(db: AsyncSession, *, days: int | None = None) -> list[dict]:
    """Every stored order with its items, newest first, as plain dicts.

    Dicts rather than ORM rows so ``orders.logic.picking_sheet`` can be unit tested without
    a database and nothing downstream can trigger lazy IO while rendering.
    """
    query = select(AmazonOrder).order_by(AmazonOrder.purchase_date_utc.desc())
    if days:
        cutoff = datetime.utcnow() - timedelta(days=days)
        query = query.where(AmazonOrder.purchase_date_utc >= cutoff)

    out: list[dict] = []
    for order in (await db.execute(query)).scalars():
        out.append({
            "amazon_order_id": order.amazon_order_id,
            "purchase_date_utc": order.purchase_date_utc,
            "latest_ship_date_utc": order.latest_ship_date_utc,
            "status": order.status,
            "easyship_status": order.easyship_status,
            "ship_service_level": order.ship_service_level,
            "order_total": float(order.order_total) if order.order_total is not None else None,
            "currency": order.currency,
            "items_ordered": int(order.items_ordered or 0),
            "items_shipped": int(order.items_shipped or 0),
            "is_prime": bool(order.is_prime),
            "is_cod": bool(order.is_cod),
            "city": order.city,
            "state": order.state,
            "postal_code": order.postal_code,
            "first_seen_at": order.first_seen_at,
            "last_refreshed_at": order.last_refreshed_at,
            "items_fetched_at": order.items_fetched_at,
            "items": [
                {
                    "asin": item.asin,
                    "seller_sku": item.seller_sku,
                    "title": item.title,
                    "quantity_ordered": int(item.quantity_ordered or 0),
                    "quantity_shipped": int(item.quantity_shipped or 0),
                    "item_price": float(item.item_price) if item.item_price is not None else None,
                }
                for item in (order.items or [])
            ],
        })
    return out


async def last_refreshed_at(db: AsyncSession) -> datetime | None:
    """When any order was last refreshed, for the "refreshed N minutes ago" banner.

    The banner matters more here than on other screens: reconciliation against a stale
    picking sheet is this feature's main failure mode, because an order shipped in Seller
    Central only leaves the sheet on the next refresh.
    """
    row = await db.execute(
        select(AmazonOrder.last_refreshed_at)
        .order_by(AmazonOrder.last_refreshed_at.desc())
        .limit(1)
    )
    return row.scalar_one_or_none()


async def purge_older_than(db: AsyncSession, days: int) -> int:
    """Delete orders purchased more than `days` ago. Returns how many went.

    Items go with them by cascade. Called from the same scheduled sweep that already prunes
    price_history, reusing DATA_RETENTION_DAYS rather than inventing a second retention
    concept.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = list(
        (
            await db.execute(
                select(AmazonOrder).where(AmazonOrder.purchase_date_utc < cutoff)
            )
        ).scalars()
    )
    for row in rows:
        await db.delete(row)
    if rows:
        await db.commit()
    return len(rows)


# ─── Packed units: the ONLY rows in this module the app writes for itself ─────
#
# Everything above is a cache of Amazon's data — refreshed, never edited. These two
# functions are the exception, and the boundary is worth keeping visible: "how many units
# are in boxes on this floor" is a fact Amazon does not have, so recording it locally is
# not a second source of truth about whether an order shipped. Nothing here writes an
# order's status and no invoice is raised from it.


async def load_packed(db: AsyncSession, pack_date: str) -> dict[str, int]:
    """`{asin: units}` packed on one IST day. Empty when nothing has been entered.

    A dict rather than rows, because every caller wants to look up one ASIN while rendering
    a line — and `dispatch_sheet` takes exactly this shape.
    """
    rows = await db.execute(
        select(OrderPackedEntry.asin, OrderPackedEntry.units)
        .where(OrderPackedEntry.pack_date == pack_date)
    )
    return {asin: int(units or 0) for asin, units in rows.all()}


async def save_packed(
    db: AsyncSession, pack_date: str, entries: list[dict]
) -> dict[str, int]:
    """Upsert packed units for one day. Returns the day's full `{asin: units}` afterwards.

    SELECT-then-UPDATE-or-INSERT, matching ``shipment.repository.save_packing_entries`` —
    the same idiom for the same two reasons: it is dialect-neutral, and the UNIQUE index on
    (pack_date, asin) is the real guarantee that a repeated save from a warehouse phone
    updates one row instead of double-counting.

    **A zero deletes the row rather than storing 0.** Correcting a mistyped count should
    remove the line, not leave a 0 that still reads as "this SKU was counted today".

    The full map is returned rather than nothing, so the screen re-renders from the committed
    truth instead of trusting what it just sent — the packer's phone can lose a response.
    """
    by_asin: dict[str, int] = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        asin = str(raw.get("asin") or "").strip().upper()
        if not asin:
            continue
        # A repeated ASIN in one payload: last wins, as on the packing screen.
        by_asin[asin] = max(0, int(raw.get("units") or 0))

    if not by_asin:
        return await load_packed(db, pack_date)

    existing = {
        row.asin: row
        for row in (
            await db.execute(
                select(OrderPackedEntry).where(
                    OrderPackedEntry.pack_date == pack_date,
                    OrderPackedEntry.asin.in_(sorted(by_asin)),
                )
            )
        ).scalars()
    }

    for asin, units in by_asin.items():
        row = existing.get(asin)
        if units == 0:
            if row is not None:
                await db.delete(row)
            continue
        if row is None:
            db.add(OrderPackedEntry(pack_date=pack_date, asin=asin, units=units))
        else:
            row.units = units

    await db.commit()
    return await load_packed(db, pack_date)


# ─── Per-ORDER packed ticks ───────────────────────────────────────────────────
#
# A different question from the units above, and it needs a different key. `load_packed`
# answers "how many 500 g pouches are boxed" (per ASIN); these answer "is order
# 407-2831377-6251535 finished" (per order id). An order holding two products contributes to
# two ASIN rows and neither one knows the parcel is incomplete — measured on 2026-08-27, 85
# orders produced 86 item lines, so one parcel that day needed both lines packed before it
# could go.


async def load_order_packed(db: AsyncSession, pack_date: str) -> dict[str, dict]:
    """`{order_id: {"packed_at", "packed_by", "source"}}` for one IST day.

    A dict keyed on the order id because both callers look up one order while rendering a row,
    and because a barcode scan arrives as an order id and needs an O(1) answer.

    The VALUE is a dict rather than a bare `True`. Absence already carries "not packed", so a
    boolean would waste the row; who ticked it and when is what makes a disputed parcel
    answerable, and `source` is what will distinguish a scan from a typed tick.
    """
    rows = await db.execute(
        select(
            OrderPackedState.amazon_order_id,
            OrderPackedState.packed_at,
            OrderPackedState.packed_by,
            OrderPackedState.source,
        ).where(OrderPackedState.pack_date == pack_date)
    )
    return {
        order_id: {
            # isoformat, not the datetime: `JSONResponse` cannot serialise a datetime, and this
            # app already shipped that exact defect once. Converted HERE so every route
            # inherits the fix rather than each remembering it.
            "packed_at": packed_at.isoformat() if packed_at else None,
            "packed_by": packed_by or "",
            "source": source or "manual",
        }
        for order_id, packed_at, packed_by, source in rows.all()
    }


async def save_order_packed(
    db: AsyncSession,
    pack_date: str,
    entries: list[dict],
    *,
    packed_by: str = "",
) -> dict[str, dict]:
    """Tick or un-tick orders for one day. Returns the day's full map afterwards.

    `entries` is `[{"amazon_order_id", "packed", "source"}]`. `packed` false DELETES the row —
    absence is the single representation of "not packed", so there is no way for a stale
    timestamp to sit behind a false flag and contradict it.

    SELECT-then-UPDATE-or-INSERT rather than an `ON CONFLICT` upsert, matching every other
    write in this app: it is dialect-neutral for the deferred Postgres move, and the UNIQUE
    index on (pack_date, amazon_order_id) is the real guarantee that a phone re-sending a lost
    request updates one row instead of duplicating it.

    **A re-tick REFRESHES `packed_at`, and that is deliberate.** Ticking an order that is
    already ticked is not a no-op worth optimising away: it means someone re-checked the box,
    and the later time is the more useful fact. It also keeps a scan idempotent without
    special-casing it.

    The full map is returned, not just what was sent, so the screen re-renders from committed
    truth instead of from what it believes it saved — a warehouse phone can lose a response and
    a stale tick is what gets acted on.
    """
    wanted: dict[str, dict] = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        order_id = str(raw.get("amazon_order_id") or "").strip()
        if not order_id:
            continue
        source = str(raw.get("source") or "manual").strip().lower()
        # A repeated order id in one payload: last wins, as on the packing screen.
        wanted[order_id] = {
            # Absent `packed` means "tick it": a scanner posts an id and nothing else.
            "packed": bool(raw.get("packed", True)),
            "source": source if source in ("manual", "scan") else "manual",
        }

    if not wanted:
        return await load_order_packed(db, pack_date)

    existing = {
        row.amazon_order_id: row
        for row in (
            await db.execute(
                select(OrderPackedState).where(
                    OrderPackedState.pack_date == pack_date,
                    OrderPackedState.amazon_order_id.in_(sorted(wanted)),
                )
            )
        ).scalars()
    }

    now = datetime.utcnow()
    for order_id, want in wanted.items():
        row = existing.get(order_id)
        if not want["packed"]:
            if row is not None:
                await db.delete(row)
            continue
        if row is None:
            db.add(OrderPackedState(
                pack_date=pack_date,
                amazon_order_id=order_id,
                packed_at=now,
                packed_by=packed_by or "",
                source=want["source"],
            ))
        else:
            row.packed_at = now
            row.packed_by = packed_by or ""
            row.source = want["source"]

    await db.commit()
    return await load_order_packed(db, pack_date)


# ─── Raw stock: standing, per parent product, typed by the owner ──────────────
#
# Sits beside the packed counts above and shares their boundary note — these are OUR rows,
# not Amazon's cache. Two separate tables rather than one, because stock and packed are
# different facts entered at different moments by different people, and a shared row means
# one save can clobber the other.


async def load_raw_stock(db: AsyncSession) -> dict[str, float]:
    """`{product_name: raw_kg}` for every product with a standing quantity.

    **Floats, not `Decimal`.** `Numeric` hands back `Decimal` and `JSONResponse` cannot
    serialise it — the same defect this feature already shipped once with datetimes, found in
    a browser on production. Converting here means every route inherits the fix.
    """
    rows = await db.execute(select(ProductRawStock.product, ProductRawStock.raw_kg))
    return {product: float(raw_kg or 0) for product, raw_kg in rows.all()}


async def save_raw_stock(
    db: AsyncSession, entries: list[dict], updated_by: str = ""
) -> dict[str, float]:
    """Upsert standing raw stock. Returns the full `{product: raw_kg}` afterwards.

    SELECT-then-UPDATE-or-INSERT, the same dialect-neutral idiom as `save_packed` and
    ``shipment.repository.save_packing_entries``. The UNIQUE index on `product` is the real
    guarantee that a repeated save updates one row rather than storing a second standing
    quantity for the same product.

    **A zero is STORED, not deleted** — the opposite of `save_packed`, deliberately. There, 0
    packed and "not counted" are the same thing on a worksheet, so the row goes. Here "we have
    none" is exactly the fact that makes `to_buy` the full ordered weight, and deleting it
    would leave the row looking untouched.

    Negatives clamp to 0: a minus sign in a weight box is a typo, not stock owed.

    The full map is returned so the screen re-renders from the committed truth rather than from
    what it believes it sent.
    """
    by_product: dict[str, float] = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        product = str(raw.get("product") or "").strip()
        if not product:
            continue
        try:
            value = float(raw.get("raw_kg") or 0)
        except (TypeError, ValueError):
            # A non-numeric weight is dropped rather than stored as 0: 0 is a measurement
            # here, and inventing one from a typo would understate what must be bought.
            logger.warning("orders: ignored non-numeric raw stock for %r", product)
            continue
        # A repeated product in one payload: last wins, as on the packing screen.
        by_product[product] = max(0.0, round(value, 2))

    if not by_product:
        return await load_raw_stock(db)

    existing = {
        row.product: row
        for row in (
            await db.execute(
                select(ProductRawStock).where(
                    ProductRawStock.product.in_(sorted(by_product))
                )
            )
        ).scalars()
    }

    now = datetime.utcnow()
    for product, raw_kg in by_product.items():
        row = existing.get(product)
        if row is None:
            db.add(ProductRawStock(
                product=product, raw_kg=raw_kg, updated_at=now,
                updated_by=updated_by or None,
            ))
        else:
            row.raw_kg = raw_kg
            row.updated_at = now
            row.updated_by = updated_by or None

    await db.commit()
    return await load_raw_stock(db)
