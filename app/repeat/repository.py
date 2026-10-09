"""The only reads and writes of `customer_order_lines` and `repeat_refresh`."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CustomerOrderLine, EconomicsDaily, RepeatRefresh

_CHUNK = 400


async def save_lines(db: AsyncSession, lines: list[dict]) -> int:
    """Upsert by (order, shipment item): delete the matching rows, then insert.

    The nightly fetch overlaps seven days, so the same line arrives several times; a re-read must
    correct the row rather than add a second one, which would read as a repeat purchase.
    """
    by_key = {(l["amazon_order_id"], l["shipment_item_id"]): l for l in lines}
    keys = list(by_key)
    for i in range(0, len(keys), _CHUNK):
        chunk = keys[i:i + _CHUNK]
        wanted = set(chunk)
        found = await db.execute(
            select(CustomerOrderLine.id, CustomerOrderLine.amazon_order_id,
                   CustomerOrderLine.shipment_item_id)
            .where(CustomerOrderLine.amazon_order_id.in_({k[0] for k in chunk})))
        ids = [r.id for r in found if (r.amazon_order_id, r.shipment_item_id) in wanted]
        if ids:
            await db.execute(delete(CustomerOrderLine).where(CustomerOrderLine.id.in_(ids)))
    db.add_all(CustomerOrderLine(**line) for line in by_key.values())
    await db.commit()
    return len(by_key)


async def load_lines(db: AsyncSession, since: str) -> list[dict]:
    """Resolved lines from `since` (inclusive). An unresolved SKU has no parent and is left out."""
    rows = await db.execute(
        select(CustomerOrderLine.amazon_order_id, CustomerOrderLine.buyer_key,
               CustomerOrderLine.purchase_day, CustomerOrderLine.child_asin,
               CustomerOrderLine.parent_asin, CustomerOrderLine.units,
               CustomerOrderLine.revenue)
        .where(CustomerOrderLine.purchase_day >= since,
               CustomerOrderLine.parent_asin.is_not(None)))
    return [{"amazon_order_id": r[0], "buyer_key": r[1], "day": date.fromisoformat(r[2]),
             "child_asin": r[3], "parent_asin": r[4], "units": int(r[5] or 0),
             "revenue": float(r[6]) if r[6] is not None else None} for r in rows]


async def purge(db: AsyncSession, keep_from: str) -> int:
    result = await db.execute(
        delete(CustomerOrderLine).where(CustomerOrderLine.purchase_day < keep_from))
    await db.commit()
    return result.rowcount or 0


async def sku_map(db: AsyncSession, catalogue: dict) -> dict[str, tuple[str, str]]:
    """seller SKU -> (child ASIN, parent ASIN).

    Economics MSKU rows first: Amazon's own SKU/ASIN pairing, and the same route the Portfolio
    tab uses, so a parent here is the parent there. The MRP sheet's FBA SKU column fills gaps.
    """
    parent_of: dict[str, str] = {}
    for child, parent in await db.execute(
            select(EconomicsDaily.child_asin, func.max(EconomicsDaily.parent_asin))
            .group_by(EconomicsDaily.child_asin)):
        parent_of[child] = parent or child
    mapping: dict[str, tuple[str, str]] = {}
    for sku, child in await db.execute(
            select(EconomicsDaily.seller_sku, func.max(EconomicsDaily.child_asin))
            .where(EconomicsDaily.seller_sku != "").group_by(EconomicsDaily.seller_sku)):
        mapping[sku.strip()] = (child, parent_of.get(child, child))
    for asin, rec in (catalogue or {}).items():
        sku = (rec.get("fba_sku") or "").strip()
        if sku and sku not in mapping:
            mapping[sku] = (asin, parent_of.get(asin, asin))
    return mapping


async def resolve_missing(db: AsyncSession, mapping: dict[str, tuple[str, str]]) -> int:
    """Fill lines stored with an unknown SKU, once the map knows it. Resolved rows are untouched."""
    rows = (await db.execute(
        select(CustomerOrderLine.id, CustomerOrderLine.seller_sku)
        .where(CustomerOrderLine.parent_asin.is_(None)))).all()
    fixed = 0
    for row_id, sku in rows:
        hit = mapping.get((sku or "").strip())
        if hit:
            await db.execute(update(CustomerOrderLine).where(CustomerOrderLine.id == row_id)
                             .values(child_asin=hit[0], parent_asin=hit[1]))
            fixed += 1
    await db.commit()
    return fixed


async def units_by_child(db: AsyncSession, start: str, end: str):
    """(FBA units from our lines, ALL-channel units from economics), keyed (parent, child).

    Per child so the caller can group them exactly as it groups the rows, including a parent
    split into its flavours."""
    fba = {(p, c): v for p, c, v in await db.execute(
        select(CustomerOrderLine.parent_asin, CustomerOrderLine.child_asin,
               func.sum(CustomerOrderLine.units))
        .where(CustomerOrderLine.purchase_day.between(start, end),
               CustomerOrderLine.parent_asin.is_not(None))
        .group_by(CustomerOrderLine.parent_asin, CustomerOrderLine.child_asin))}
    parent = func.coalesce(EconomicsDaily.parent_asin, EconomicsDaily.child_asin)
    allc = {(p, c): v for p, c, v in await db.execute(
        select(parent, EconomicsDaily.child_asin, func.sum(EconomicsDaily.units_ordered))
        .where(EconomicsDaily.day.between(start, end), EconomicsDaily.seller_sku == "")
        .group_by(parent, EconomicsDaily.child_asin))}
    return ({k: int(v or 0) for k, v in fba.items()}, {k: int(v or 0) for k, v in allc.items()})


async def record_run(db: AsyncSession, **fields) -> None:
    db.add(RepeatRefresh(finished_at=datetime.utcnow(), **fields))
    await db.commit()


async def done_runs(db: AsyncSession) -> list[tuple[str, str]]:
    rows = await db.execute(select(RepeatRefresh.window_start, RepeatRefresh.window_end)
                            .where(RepeatRefresh.status == "done"))
    return [(a, b) for a, b in rows if a and b]


async def last_run(db: AsyncSession, ending_from: str | None = None) -> dict | None:
    """The newest run, or the newest one whose window reaches `ending_from` or later.

    The filter is what keeps an OLD backfill chunk from speaking for today's data. On 8 Oct a
    chunk for Dec-Jan was refused on quota after every needed day was already stored, and the
    screen then led with "The last refresh failed" above figures that were complete.
    """
    query = select(RepeatRefresh)
    if ending_from:
        query = query.where(RepeatRefresh.window_end >= ending_from)
    row = (await db.execute(query.order_by(RepeatRefresh.id.desc()).limit(1))
           ).scalar_one_or_none()
    if not row:
        return None
    return {"window": [row.window_start, row.window_end], "status": row.status,
            "error": row.error, "lines_stored": row.lines_stored,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}



async def econ_by_month(db: AsyncSession):
    """Ad spend (SP + attributed SB) and ALL-channel units per (month, Amazon parent, child), and the
    days held — for Customer value's CAC. ASIN grain only, so the MSKU split is never double-counted."""
    parent = func.coalesce(EconomicsDaily.parent_asin, EconomicsDaily.child_asin)
    month = func.substr(EconomicsDaily.day, 1, 7)
    rows = await db.execute(
        select(month, parent, EconomicsDaily.child_asin,
               func.sum(EconomicsDaily.ad_spend), func.sum(EconomicsDaily.sb_spend),
               func.sum(EconomicsDaily.units_ordered))
        .where(EconomicsDaily.seller_sku == "")
        .group_by(month, parent, EconomicsDaily.child_asin))
    out = {(m, p, c): (float(a or 0) + float(sb or 0), int(u or 0)) for m, p, c, a, sb, u in rows}
    days = {d for (d,) in await db.execute(
        select(EconomicsDaily.day).where(EconomicsDaily.seller_sku == "").distinct())}
    return out, days


#: A day counts as priced when at most this share of its lines lacks a price. Not zero: a report
#: row can arrive with a blank price (unknown, stored NULL), and one such line must not hold every
#: LTV back for ever. Lines stored before the price was read are ~100% of their days.
UNPRICED_TOLERANCE = 0.02


async def priced_from(db: AsyncSession, since: str) -> str | None:
    """The first day after the last UNPRICED day, or None when no day is priced yet.

    Lines stored before the price was read have `revenue` NULL until the backfill re-reads them; an
    LTV over a span containing them would read short with nothing on screen saying so."""
    rows = (await db.execute(
        select(CustomerOrderLine.purchase_day, func.count(),
               func.sum(case((CustomerOrderLine.revenue.is_(None), 1), else_=0)))
        .where(CustomerOrderLine.purchase_day >= since, CustomerOrderLine.parent_asin.is_not(None))
        .group_by(CustomerOrderLine.purchase_day))).all()
    if not rows:
        return None
    unpriced = [d for d, n, missing in rows if (missing or 0) > UNPRICED_TOLERANCE * n]
    if not unpriced:
        # Every day that HAS lines is priced, so the whole span is: a day without orders has
        # nothing unpriced in it. Returning the first day with lines instead would push a cohort
        # whose month began before it out of every LTV.
        return since
    last = max(unpriced)
    if last >= max(d for d, _, _ in rows):
        return None
    return (date.fromisoformat(last) + timedelta(days=1)).isoformat()
