"""Database access for the shipment workflow.

Two responsibilities that are worth stating, because both are load-bearing:

**1. One ORDER BY.** ``load_plan_items()`` is the only SELECT of plan items in
the codebase. Every endpoint and all four document builders go through it, so the
dashboard and the downloads physically cannot disagree about row order. The
requested ordering is product-then-weight; ``ShipmentPlanItem.sort_product``
holds the casefolded product name so SQL reproduces
``app.shipment.logic.sort_key`` exactly.

**2. Write separation instead of locking.** The owner writes plan rows, ops
writes packing rows, and no function here writes both. That is what makes two
concurrent users safe without a version column: there is no shared row to
clobber. The two UNIQUE indexes (plan+date, day+asin) turn a repeated save into
an update rather than a duplicate, so a flaky warehouse connection cannot
double-count units.

Upserts are written as SELECT-then-UPDATE-or-INSERT rather than
``ON CONFLICT``/``ON DUPLICATE KEY``, so the same code runs on SQLite locally and
PostgreSQL in production.
"""
import logging
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ShipmentPackingDay,
    ShipmentPackingEntry,
    ShipmentPlan,
    ShipmentPlanItem,
)
from app.shipment import logic

logger = logging.getLogger(__name__)

# Fields the owner may change on a plan item after /generate. Everything else on
# the row is a snapshot of the CSV upload and must keep showing the numbers the
# plan was built from, so an edit cannot rewrite history.
EDITABLE_ITEM_FIELDS = ("shipment_plan", "available", "s", "m", "b", "fba_sku")


# ─── Plans ───────────────────────────────────────────────────────────────────

async def get_active_plan(db: AsyncSession) -> ShipmentPlan | None:
    """The plan currently being packed, or None.

    Ordered by id descending so that if two plans are somehow both active (a
    crash between closing the old one and creating the new one), the newest wins
    rather than the query returning an arbitrary row.
    """
    result = await db.execute(
        select(ShipmentPlan)
        .where(ShipmentPlan.status == "active")
        .order_by(ShipmentPlan.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_plan(db: AsyncSession, plan_id: int) -> ShipmentPlan | None:
    result = await db.execute(select(ShipmentPlan).where(ShipmentPlan.id == plan_id))
    return result.scalar_one_or_none()


async def close_active_plans(db: AsyncSession) -> int:
    """Mark every active plan closed. Returns how many were closed.

    Called before creating a new plan. Closed plans are kept, not deleted — they
    carry the packing history the invoices were generated from.
    """
    result = await db.execute(select(ShipmentPlan).where(ShipmentPlan.status == "active"))
    plans = list(result.scalars())
    for plan in plans:
        plan.status = "closed"
    return len(plans)


async def create_plan(
    db: AsyncSession,
    items: list[dict],
    multiplier: float = 5.0,
    label: str | None = None,
    min_cartons: int = logic.DEFAULT_MIN_CARTONS,
    min_units: int = logic.DEFAULT_MIN_UNITS,
) -> ShipmentPlan:
    """Create a plan and its item rows, closing any previous active plan.

    ``items`` are plain dicts straight from the CSV parsing step. Rounding has
    already happened in the router (once, at generate time) — this function
    stores what it is given so a manual override of 437 survives verbatim.
    """
    await close_active_plans(db)

    plan = ShipmentPlan(
        label=label or f"Plan {datetime.utcnow():%Y-%m-%d}",
        multiplier=multiplier,
        status="active",
        min_cartons=min_cartons,
        min_units=min_units,
    )
    db.add(plan)
    await db.flush()  # need plan.id before adding items

    for raw in items:
        product = str(raw.get("item") or "")
        db.add(
            ShipmentPlanItem(
                plan_id=plan.id,
                asin=raw.get("asin") or "",
                fba_sku=raw.get("fba_sku") or "",
                brand=raw.get("brand") or "",
                item=product,
                # Persisted casefolded so ORDER BY matches logic.sort_key.
                sort_product=product.casefold()[:120],
                weight=raw.get("weight") or 0,
                sales_7d=int(raw.get("sales_7d") or 0),
                projection=int(raw.get("projection") or 0),
                fba_stock=int(raw.get("fba_stock") or 0),
                deficit=int(raw.get("deficit") or 0),
                shipment_plan=int(raw.get("shipment_plan") or 0),
                available=int(raw.get("available") or 0),
                s=bool(raw.get("s")),
                m=bool(raw.get("m")),
                b=bool(raw.get("b")),
            )
        )

    await db.commit()
    await db.refresh(plan)
    return plan


async def delete_plan(db: AsyncSession, plan_id: int) -> bool:
    """Delete a plan and, by cascade, its items, days and packing entries."""
    plan = await get_plan(db, plan_id)
    if plan is None:
        return False
    await db.delete(plan)
    await db.commit()
    return True


async def update_thresholds(
    db: AsyncSession,
    plan_id: int,
    min_cartons: int | None = None,
    min_units: int | None = None,
) -> ShipmentPlan | None:
    plan = await get_plan(db, plan_id)
    if plan is None:
        return None
    if min_cartons is not None:
        plan.min_cartons = max(0, int(min_cartons))
    if min_units is not None:
        plan.min_units = max(0, int(min_units))
    await db.commit()
    await db.refresh(plan)
    return plan


# ─── Plan items ──────────────────────────────────────────────────────────────

async def load_plan_items(db: AsyncSession, plan_id: int) -> list[ShipmentPlanItem]:
    """Every item in the plan, ordered product-then-weight-then-ASIN.

    THE single source of row order. Callers must render this list as-is; the
    frontend JS and every document builder rely on that, and
    tests/test_shipment_documents.py asserts a downloaded sheet's row order
    equals this order. Sorting anywhere else is how the screen and the download
    drift apart.

    ASIN is the last tiebreak only for determinism — without it two SKUs of the
    same product and weight could swap places between two requests.
    """
    result = await db.execute(
        select(ShipmentPlanItem)
        .where(ShipmentPlanItem.plan_id == plan_id)
        .order_by(
            ShipmentPlanItem.sort_product,
            ShipmentPlanItem.weight,
            ShipmentPlanItem.asin,
        )
    )
    return list(result.scalars())


async def update_plan_items(
    db: AsyncSession, plan_id: int, updates: list[dict]
) -> int:
    """Apply owner edits to plan items, keyed by ASIN. Returns rows changed.

    Only EDITABLE_ITEM_FIELDS are written, and packing fields are not among
    them — this function is reachable only by admin, and it must be incapable of
    touching what ops recorded even if the frontend posts a stale full row.
    """
    if not updates:
        return 0

    by_asin = {
        str(u.get("asin")): u for u in updates if isinstance(u, dict) and u.get("asin")
    }
    if not by_asin:
        return 0

    result = await db.execute(
        select(ShipmentPlanItem).where(
            ShipmentPlanItem.plan_id == plan_id,
            ShipmentPlanItem.asin.in_(list(by_asin)),
        )
    )

    changed = 0
    for item in result.scalars():
        payload = by_asin.get(item.asin)
        if not payload:
            continue
        touched = False
        for field in EDITABLE_ITEM_FIELDS:
            if field not in payload:
                continue
            value = payload[field]
            if field in ("s", "m", "b"):
                value = bool(value)
            elif field == "fba_sku":
                value = str(value or "")
            else:
                try:
                    value = max(0, int(value or 0))
                except (TypeError, ValueError):
                    continue
            if getattr(item, field) != value:
                setattr(item, field, value)
                touched = True
        if touched:
            changed += 1

    await db.commit()
    return changed


async def count_items_missing_sku(db: AsyncSession, plan_id: int) -> int:
    """How many planned SKUs have no merchant SKU.

    Surfaced rather than silently tolerated: Amazon's shipment upload keys on the
    merchant SKU, so a row that falls back to its ASIN is rejected on their side.
    The old code filled fba_sku inside a bare `except Exception: pass`, so this
    failed invisibly.
    """
    result = await db.execute(
        select(func.count())
        .select_from(ShipmentPlanItem)
        .where(
            ShipmentPlanItem.plan_id == plan_id,
            ShipmentPlanItem.shipment_plan > 0,
            (ShipmentPlanItem.fba_sku.is_(None)) | (ShipmentPlanItem.fba_sku == ""),
        )
    )
    return int(result.scalar() or 0)


# ─── Packing days ────────────────────────────────────────────────────────────

async def load_days(db: AsyncSession, plan_id: int) -> list[ShipmentPackingDay]:
    """Every packing day for the plan, oldest first (chronological columns)."""
    result = await db.execute(
        select(ShipmentPackingDay)
        .where(ShipmentPackingDay.plan_id == plan_id)
        .order_by(ShipmentPackingDay.pack_date)
    )
    return list(result.scalars())


async def load_held_days(db: AsyncSession, plan_id: int) -> list[ShipmentPackingDay]:
    """Days parked by the threshold, oldest first.

    Used to warn before a plan is replaced. ``/active`` only ever shows the
    active plan, so a day held on Saturday disappears from every screen the
    moment Monday's plan is generated — the boxes are still in the warehouse but
    nothing in the app mentions them again. That is precisely the "held stock
    becomes lost stock" failure the hold was introduced to prevent, so the owner
    is told before it happens rather than discovering it at stock-take.
    """
    result = await db.execute(
        select(ShipmentPackingDay)
        .where(
            ShipmentPackingDay.plan_id == plan_id,
            ShipmentPackingDay.status == logic.STATUS_HELD,
        )
        .order_by(ShipmentPackingDay.pack_date)
    )
    return list(result.scalars())


async def get_day(
    db: AsyncSession, plan_id: int, pack_date: str
) -> ShipmentPackingDay | None:
    result = await db.execute(
        select(ShipmentPackingDay).where(
            ShipmentPackingDay.plan_id == plan_id,
            ShipmentPackingDay.pack_date == pack_date,
        )
    )
    return result.scalar_one_or_none()


async def get_or_create_day(
    db: AsyncSession, plan_id: int, pack_date: str
) -> ShipmentPackingDay:
    """Fetch the day, creating it `open` if this is the first entry for it.

    SELECT-then-INSERT rather than a dialect-specific upsert. The UNIQUE index on
    (plan_id, pack_date) is the real guarantee: if two requests race, the loser's
    INSERT fails and we re-read the winner's row instead of ending up with two
    days for one date.
    """
    day = await get_day(db, plan_id, pack_date)
    if day is not None:
        return day

    day = ShipmentPackingDay(
        plan_id=plan_id, pack_date=pack_date, status=logic.STATUS_OPEN
    )
    db.add(day)
    try:
        await db.flush()
    except Exception:
        # Lost the race against a concurrent first-save for the same date.
        await db.rollback()
        existing = await get_day(db, plan_id, pack_date)
        if existing is None:
            raise
        return existing
    return day


async def save_packing_entries(
    db: AsyncSession,
    plan_id: int,
    pack_date: str,
    entries: list[dict],
    submitted_by: str = "ops",
) -> ShipmentPackingDay:
    """Upsert units and cartons for a day. The only write ops performs.

    A zero-unit, zero-carton entry is deleted rather than stored, so correcting a
    mistyped row actually removes it from the totals instead of leaving a 0 that
    still counts as "this SKU was touched".

    Totals are denormalised onto the day so the hold check and the day list never
    have to load every entry.
    """
    day = await get_or_create_day(db, plan_id, pack_date)

    by_asin: dict[str, dict] = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        asin = str(raw.get("asin") or "").strip()
        if not asin:
            continue
        by_asin[asin] = raw  # a repeated ASIN in one payload: last wins

    existing_result = await db.execute(
        select(ShipmentPackingEntry).where(ShipmentPackingEntry.day_id == day.id)
    )
    existing = {row.asin: row for row in existing_result.scalars()}

    for asin, raw in by_asin.items():
        units = max(0, _as_int(raw.get("units")))
        cartons = max(0, _as_int(raw.get("cartons")))
        note = raw.get("note") or None
        row = existing.get(asin)

        if units == 0 and cartons == 0:
            if row is not None:
                await db.delete(row)
            continue

        if row is None:
            db.add(
                ShipmentPackingEntry(
                    day_id=day.id, asin=asin, units=units, cartons=cartons, note=note
                )
            )
        else:
            row.units = units
            row.cartons = cartons
            row.note = note

    await db.flush()
    await _recompute_day_totals(db, day)
    day.submitted_by = submitted_by
    await db.commit()
    await db.refresh(day)
    return day


async def _recompute_day_totals(db: AsyncSession, day: ShipmentPackingDay) -> None:
    """Recompute the denormalised totals from the entry rows.

    Deliberately a fresh SUM rather than incremental arithmetic: an incremental
    counter drifts the moment any write path forgets to adjust it, and these
    totals decide whether a day is held.
    """
    result = await db.execute(
        select(
            func.coalesce(func.sum(ShipmentPackingEntry.units), 0),
            func.coalesce(func.sum(ShipmentPackingEntry.cartons), 0),
        ).where(ShipmentPackingEntry.day_id == day.id)
    )
    units, cartons = result.one()
    day.total_units = int(units or 0)
    day.total_cartons = int(cartons or 0)


async def load_entries(db: AsyncSession, day_id: int) -> list[ShipmentPackingEntry]:
    result = await db.execute(
        select(ShipmentPackingEntry)
        .where(ShipmentPackingEntry.day_id == day_id)
        .order_by(ShipmentPackingEntry.asin)
    )
    return list(result.scalars())


async def load_days_with_entries(db: AsyncSession, plan_id: int) -> list[dict]:
    """Days plus their entries, shaped for the pure functions in logic.py.

    Returns dicts rather than ORM objects so the aggregation helpers can be unit
    tested without a database, and so nothing downstream can lazily trigger IO.
    """
    days = await load_days(db, plan_id)
    out: list[dict] = []
    for day in days:
        entries = await load_entries(db, day.id)
        out.append(
            {
                "id": day.id,
                "pack_date": day.pack_date,
                "status": day.status,
                "hold_reason": day.hold_reason,
                "total_units": int(day.total_units or 0),
                "total_cartons": int(day.total_cartons or 0),
                "submitted_by": day.submitted_by,
                "submitted_at": day.submitted_at.isoformat() if day.submitted_at else None,
                "verified_at": day.verified_at.isoformat() if day.verified_at else None,
                "invoice_id": day.invoice_id,
                "entries": [
                    {
                        "asin": e.asin,
                        "units": int(e.units or 0),
                        "cartons": int(e.cartons or 0),
                        "note": e.note,
                    }
                    for e in entries
                ],
            }
        )
    return out


async def clear_day_entries(db: AsyncSession, day_id: int) -> None:
    await db.execute(
        delete(ShipmentPackingEntry).where(ShipmentPackingEntry.day_id == day_id)
    )
    await db.commit()


def _as_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
