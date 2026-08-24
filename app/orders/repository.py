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

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AmazonOrder, AmazonOrderItem

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


async def ids_missing_items(db: AsyncSession, limit: int = 200) -> list[str]:
    """Order ids whose line items have never been fetched, oldest first.

    This is what keeps a daily refresh cheap: `getOrderItems` costs a call per order, so
    re-fetching 100 known orders would spend 100 calls to learn nothing — over three
    minutes at the interval the client uses.

    Capped, because a first run against 90 days of history would otherwise queue hundreds
    of calls into one job. The remainder is picked up by the next refresh.
    """
    rows = await db.execute(
        select(AmazonOrder.amazon_order_id)
        .where(AmazonOrder.items_fetched_at.is_(None))
        .order_by(AmazonOrder.purchase_date_utc.desc())
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
