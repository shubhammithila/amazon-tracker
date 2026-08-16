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
    Invoice,
    ProductCategory,
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

# Plan lifecycle. `draft` is owner-only; only `active` is visible to the packer,
# and get_active_plan() matching `active` alone is what enforces that across every
# pre-existing endpoint without touching them.
STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"


# ─── Plans ───────────────────────────────────────────────────────────────────

async def get_active_plan(db: AsyncSession) -> ShipmentPlan | None:
    """The plan currently being packed, or None.

    **Matches `active` and nothing else, and that is a load-bearing omission.**
    Drafts are invisible here, which is what makes all eleven pre-existing packing
    and download endpoints draft-safe without a single edit to any of them. Widen
    this and the warehouse starts packing plans the owner has not finished.

    Ordered by id descending so that if two plans are somehow both active (a crash
    between closing the old one and activating the new one), the newest wins rather
    than the query returning an arbitrary row.
    """
    result = await db.execute(
        select(ShipmentPlan)
        .where(ShipmentPlan.status == STATUS_ACTIVE)
        .order_by(ShipmentPlan.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_plan(db: AsyncSession, plan_id: int) -> ShipmentPlan | None:
    result = await db.execute(select(ShipmentPlan).where(ShipmentPlan.id == plan_id))
    return result.scalar_one_or_none()


async def close_active_plans(db: AsyncSession) -> int:
    """Mark every active plan closed. Returns how many were closed.

    Called by ``finalise_plan``, NOT by ``create_plan`` — see that function's
    docstring for why. Closed plans are kept, not deleted: they carry the packing
    history the invoices were generated from.
    """
    result = await db.execute(
        select(ShipmentPlan).where(ShipmentPlan.status == STATUS_ACTIVE)
    )
    plans = list(result.scalars())
    for plan in plans:
        plan.status = STATUS_CLOSED
    return len(plans)


async def create_plan(
    db: AsyncSession,
    items: list[dict],
    multiplier: float = 5.0,
    label: str | None = None,
    min_cartons: int = logic.DEFAULT_MIN_CARTONS,
    min_units: int = logic.DEFAULT_MIN_UNITS,
    status: str = STATUS_DRAFT,
) -> ShipmentPlan:
    """Create a plan and its item rows. Does NOT touch the current active plan.

    **It used to call ``close_active_plans()`` and must not.** With drafts, that
    would mean uploading a CSV instantly closed the plan the warehouse was packing
    — the packer's screen would empty mid-shift, with no warning and nothing to
    explain it, while the replacement sat invisible in draft. Closing is now
    ``finalise_plan``'s job, which is the moment the owner actually decides.

    ``items`` are plain dicts straight from the CSV parsing step. Rounding has
    already happened in the router (once, at generate time) — this function
    stores what it is given so a manual override of 437 survives verbatim.
    """
    plan = ShipmentPlan(
        label=label or f"Plan {datetime.utcnow():%Y-%m-%d}",
        multiplier=multiplier,
        status=status,
        min_cartons=min_cartons,
        min_units=min_units,
    )
    db.add(plan)
    await db.flush()  # need plan.id before adding items

    seen_products: dict[str, str] = {}
    for raw in items:
        product = str(raw.get("item") or "")
        key = product.casefold()[:120]
        if key:
            seen_products.setdefault(key, product)
        db.add(
            ShipmentPlanItem(
                plan_id=plan.id,
                asin=raw.get("asin") or "",
                fba_sku=raw.get("fba_sku") or "",
                brand=raw.get("brand") or "",
                item=product,
                # Persisted casefolded so ORDER BY matches logic.sort_key, and
                # doubles as the join key into product_categories.
                sort_product=key,
                # Persisted because 'MF'/'HF' cannot order alphabetically.
                brand_rank=logic.brand_rank_for(raw.get("brand")),
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

    # Seed keyword categories for any product not classified yet, AFTER the
    # commit so a failure here cannot lose the plan. Existing rows are untouched,
    # so a re-upload never reverts a priority the owner set by hand.
    await ensure_categories(db, seen_products)
    return plan


async def finalise_plan(db: AsyncSession, plan_id: int) -> ShipmentPlan | None:
    """Promote a draft to active, closing whatever was active before.

    The close lives HERE rather than in ``create_plan`` on purpose. This is the
    single moment the owner decides the new plan replaces the old one, so it is
    the only moment the warehouse's plan should change under them. Both writes
    happen in one transaction: a crash between them would leave either two active
    plans or none, and ``get_active_plan``'s ``id desc`` only papers over the
    first of those.

    Returns None if there is no such plan. An already-active plan is returned
    unchanged, so a double-click is harmless.
    """
    plan = await get_plan(db, plan_id)
    if plan is None:
        return None
    if plan.status == STATUS_ACTIVE:
        return plan

    result = await db.execute(
        select(ShipmentPlan).where(
            ShipmentPlan.status == STATUS_ACTIVE, ShipmentPlan.id != plan_id
        )
    )
    for previous in result.scalars():
        previous.status = STATUS_CLOSED

    plan.status = STATUS_ACTIVE
    await db.commit()
    await db.refresh(plan)
    return plan


async def get_draft_plan(db: AsyncSession) -> ShipmentPlan | None:
    """The plan being prepared, if any. Owner-only by construction.

    Separate from ``get_active_plan`` deliberately: that function is unchanged and
    still matches only `active`, which is what keeps every pre-existing packing
    endpoint blind to drafts without touching any of them.
    """
    result = await db.execute(
        select(ShipmentPlan)
        .where(ShipmentPlan.status == STATUS_DRAFT)
        .order_by(ShipmentPlan.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def delete_draft_plans(db: AsyncSession) -> int:
    """Drop existing drafts before creating a new one.

    Safe because no packing endpoint can reach a draft, so a draft can never
    carry packing rows worth keeping. Without this, generating twice would leave
    an orphan draft that `get_draft_plan` would then have to choose between.
    """
    result = await db.execute(
        select(ShipmentPlan).where(ShipmentPlan.status == STATUS_DRAFT)
    )
    drafts = list(result.scalars())
    for draft in drafts:
        await db.delete(draft)
    if drafts:
        await db.commit()
    return len(drafts)


async def set_item_excluded(
    db: AsyncSession, plan_id: int, asins: list[str], excluded: bool
) -> list[str]:
    """Exclude or restore plan rows. Returns the ASINs actually changed.

    Reversible by design — ``excluded_at`` is set or cleared, nothing is deleted,
    so an accidental multi-row exclude is one click back.
    """
    if not asins:
        return []

    result = await db.execute(
        select(ShipmentPlanItem).where(
            ShipmentPlanItem.plan_id == plan_id,
            ShipmentPlanItem.asin.in_(list(asins)),
        )
    )
    stamp = datetime.utcnow() if excluded else None
    changed = []
    for item in result.scalars():
        if excluded and item.excluded_at is not None:
            continue
        if not excluded and item.excluded_at is None:
            continue
        item.excluded_at = stamp
        changed.append(item.asin)

    if changed:
        await db.commit()
    return changed


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

async def load_plan_items(
    db: AsyncSession, plan_id: int, *, include_excluded: bool = False
) -> list[ShipmentPlanItem]:
    """Every item in the plan, in canonical order.

        brand → category priority → product → weight → ASIN

    THE single source of row order. Callers must render this list as-is; the
    frontend JS and every document builder rely on that, and
    tests/test_shipment_documents.py asserts a downloaded sheet's row order equals
    this order. Sorting anywhere else is how the screen and the downloads drift
    apart — which is the complaint that produced the sorting requirement.

    **The category comes from a JOIN, not from a column on this row.** Priority
    lives in ``product_categories`` keyed by product, so re-classifying a product
    changes every plan's order immediately and needs no row rewrite. A
    denormalised rank or a stored composite sort key would go stale the moment the
    owner edited a category, and a stale sort key misorders silently.

    ``COALESCE(priority, 6)`` is a safety net only: categories are materialised at
    generate time by ``ensure_categories``. A product with no row still sorts,
    into "Rest", rather than vanishing or raising.

    Product sits ABOVE weight so every size of one product is contiguous — the
    packer picks one product from one location and finishes it.

    ``include_excluded`` defaults to False, and the default is the safety
    property: forgetting the flag hides a row (cosmetic) rather than leaking an
    excluded SKU onto the packer's sheet, the Amazon upload or a GST invoice. Only
    two callers should ever pass True — the owner's "show excluded" toggle and the
    invoice bridge's defensive check.

    ASIN is the last tiebreak only for determinism — without it two SKUs of the
    same product and weight could swap places between two requests.
    """
    priority = func.coalesce(ProductCategory.priority, logic.DEFAULT_CATEGORY)

    query = (
        select(ShipmentPlanItem, priority.label("category_rank"))
        .outerjoin(
            ProductCategory,
            ProductCategory.product_key == ShipmentPlanItem.sort_product,
        )
        .where(ShipmentPlanItem.plan_id == plan_id)
    )
    if not include_excluded:
        query = query.where(ShipmentPlanItem.excluded_at.is_(None))

    query = query.order_by(
        ShipmentPlanItem.brand_rank,
        priority,
        ShipmentPlanItem.sort_product,
        ShipmentPlanItem.weight,
        ShipmentPlanItem.asin,
    )

    items: list[ShipmentPlanItem] = []
    for item, category_rank in await db.execute(query):
        # Attached as a transient attribute so logic.sort_key reads the SAME rank
        # SQL just ordered by. Without this the pure function would re-derive it
        # from the product name and silently ignore the owner's override, and the
        # two would disagree — which is exactly what
        # tests/test_shipment_plan_db.py::test_the_sql_order_matches_the_pure_sort_function
        # exists to catch.
        item.category_rank = int(category_rank or logic.DEFAULT_CATEGORY)
        items.append(item)
    return items


async def ensure_categories(db: AsyncSession, products: dict[str, str]) -> int:
    """Seed keyword-default categories for products that have none yet.

    ``products`` maps the casefolded key to the label as it should read on screen.
    Existing rows are left completely alone — that is the point. A re-upload must
    never quietly revert a priority the owner set by hand, so this only ever
    INSERTs.

    Returns how many were created.
    """
    if not products:
        return 0

    existing = set(
        (
            await db.execute(
                select(ProductCategory.product_key).where(
                    ProductCategory.product_key.in_(list(products))
                )
            )
        ).scalars()
    )

    created = 0
    for key, label in products.items():
        if not key or key in existing:
            continue
        db.add(
            ProductCategory(
                product_key=key,
                product_label=label or key,
                priority=logic.category_for(label or key),
                source="keyword",
            )
        )
        created += 1

    if created:
        await db.commit()
    return created


async def load_categories(db: AsyncSession) -> list[ProductCategory]:
    """Every known product category, for the priority editor."""
    result = await db.execute(
        select(ProductCategory).order_by(
            ProductCategory.priority, ProductCategory.product_key
        )
    )
    return list(result.scalars())


async def set_categories(db: AsyncSession, updates: dict[str, int]) -> int:
    """Apply owner overrides. Marks them `manual` so the UI can show which.

    Creates a row if the product has none, so a category can be set for a product
    that predates the table.
    """
    changed = 0
    for raw_key, raw_priority in (updates or {}).items():
        key = str(raw_key or "").strip().casefold()
        if not key:
            continue
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            continue
        if priority not in logic.CATEGORY_LABELS:
            continue

        row = (
            await db.execute(
                select(ProductCategory).where(ProductCategory.product_key == key)
            )
        ).scalar_one_or_none()

        if row is None:
            db.add(
                ProductCategory(
                    product_key=key, product_label=key,
                    priority=priority, source="manual",
                )
            )
            changed += 1
        elif row.priority != priority:
            row.priority = priority
            row.source = "manual"
            changed += 1

    if changed:
        await db.commit()
    return changed


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
            # Excluded rows are not going to Amazon, so a missing SKU on one is
            # not a problem to warn about. Counting them would nag the owner about
            # rows he has deliberately removed.
            ShipmentPlanItem.excluded_at.is_(None),
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
    cartons: int | None = None,
) -> ShipmentPackingDay:
    """Upsert per-SKU units, and the day's carton count. The only write ops performs.

    ``cartons`` is the number of boxes packed that DAY, in total — not per SKU. A
    carton on this floor is filled with whatever is being packed at the time, so it
    belongs to no single ASIN. ``None`` means "leave it alone", which is what lets
    the screen save unit counts without the packer having reached the carton box
    yet; passing 0 explicitly does clear it.

    A zero-unit entry is deleted rather than stored, so correcting a mistyped row
    removes it from the total instead of leaving a 0 that still counts as "this SKU
    was touched".

    Unit totals are denormalised onto the day so the hold check and the day list
    never have to load every entry.
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
        note = raw.get("note") or None
        row = existing.get(asin)

        if units == 0:
            if row is not None:
                await db.delete(row)
            continue

        if row is None:
            db.add(
                ShipmentPackingEntry(day_id=day.id, asin=asin, units=units, note=note)
            )
        else:
            row.units = units
            row.note = note

    await db.flush()
    await _recompute_day_units(db, day)
    if cartons is not None:
        day.total_cartons = max(0, _as_int(cartons))
    day.submitted_by = submitted_by
    await db.commit()
    await db.refresh(day)
    return day


async def _recompute_day_units(db: AsyncSession, day: ShipmentPackingDay) -> None:
    """Recompute the denormalised unit total from the entry rows.

    Deliberately a fresh SUM rather than incremental arithmetic: an incremental
    counter drifts the moment any write path forgets to adjust it, and this total
    decides whether a day is held.

    **It must not touch total_cartons.** That is now entered directly rather than
    summed, so recomputing it from the entries would zero the packer's carton count
    on every save — silently, and the number feeds a GST invoice's Boxes field.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(ShipmentPackingEntry.units), 0)).where(
            ShipmentPackingEntry.day_id == day.id
        )
    )
    day.total_units = int(result.scalar() or 0)


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

    # The GST invoice NUMBER for each attached invoice, resolved in one query rather than
    # per day. The days store an `invoice_id`, which is a row id — showing that puts "On
    # invoice #46" on screen, and 46 matches nothing the owner can search for, because the
    # document he holds says "ST/26-27/046".
    invoice_ids = {d.invoice_id for d in days if d.invoice_id}
    invoice_numbers: dict[int, str] = {}
    if invoice_ids:
        rows = await db.execute(
            select(Invoice.id, Invoice.invoice_no).where(Invoice.id.in_(invoice_ids))
        )
        invoice_numbers = {row.id: row.invoice_no for row in rows}

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
                # None when the invoice row has since been deleted, which the screen
                # falls back from rather than rendering "undefined" on a day card.
                "invoice_no": invoice_numbers.get(day.invoice_id),
                # The Amazon shipment these boxes went into. `shipment_confirmation_id` is
                # the FBA15… string that reaches the GST invoice; the destination is what
                # Amazon actually CHOSE, which is not necessarily the FC that was requested
                # and is what decides the recipient GSTIN.
                "inbound_plan_id": day.inbound_plan_id,
                "amazon_shipment_id": day.amazon_shipment_id,
                "shipment_confirmation_id": day.shipment_confirmation_id,
                "destination_warehouse_id": day.destination_warehouse_id,
                "destination_state": day.destination_state,
                # Units only. Cartons are a day-level fact and are already above as
                # `total_cartons`; a per-entry key here would invite summing it back
                # up into a number that means nothing.
                "entries": [
                    {"asin": e.asin, "units": int(e.units or 0), "note": e.note}
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


# ─── The Amazon shipment on a set of days ────────────────────────────────────
#
# Written by the SP-API routes. Separate small functions rather than one flexible one,
# because they happen at different moments and the ORDER is the safety property: the plan
# id is written BEFORE placement is confirmed, and the confirmation id only after Amazon
# has actually created the shipment.


async def attach_inbound_plan(
    db: AsyncSession, plan_id: int, pack_dates: list[str], inbound_plan_id: str
) -> int:
    """Record the Amazon inbound plan id against the chosen days.

    Called **before** `confirmPlacementOption`, and that ordering is the whole point. A plan
    confirmed at Amazon with no local record is invisible to this app and entirely real to
    them — the same failure the invoice/attach window is documented for, except a shipment
    cannot be reconciled by re-running anything.

    Committed immediately, not left to a later flush, so a crash between here and the
    confirm still leaves the id recoverable.
    """
    if not pack_dates:
        return 0
    result = await db.execute(
        select(ShipmentPackingDay).where(
            ShipmentPackingDay.plan_id == plan_id,
            ShipmentPackingDay.pack_date.in_(pack_dates),
        )
    )
    days = list(result.scalars())
    for day in days:
        day.inbound_plan_id = inbound_plan_id
    await db.commit()
    return len(days)


async def attach_amazon_shipment(
    db: AsyncSession,
    plan_id: int,
    pack_dates: list[str],
    *,
    inbound_plan_id: str,
    amazon_shipment_id: str,
    confirmation_id: str,
    warehouse_id: str,
    state: str,
) -> int:
    """Record the confirmed shipment: its FBA id and the destination Amazon chose.

    Keyword-only past the dates because five strings in a row is exactly where an
    `amazon_shipment_id` ends up in the `confirmation_id` column — and those two are
    different identifiers for the same shipment (`shcc4552…` versus `FBA15M59XQFZ`), so a
    swap would produce a label lookup that fails and an invoice carrying a string the owner
    has never seen.

    `warehouse_id` and `state` come from Amazon's answer, never from the FC that was
    requested: the destination decides which of the 15 GSTINs the invoice must use.
    """
    if not pack_dates:
        return 0
    result = await db.execute(
        select(ShipmentPackingDay).where(
            ShipmentPackingDay.plan_id == plan_id,
            ShipmentPackingDay.pack_date.in_(pack_dates),
        )
    )
    days = list(result.scalars())
    for day in days:
        day.inbound_plan_id = inbound_plan_id
        day.amazon_shipment_id = amazon_shipment_id
        day.shipment_confirmation_id = confirmation_id
        day.destination_warehouse_id = warehouse_id
        day.destination_state = state
    await db.commit()
    return len(days)


async def clear_inbound_plan(
    db: AsyncSession, plan_id: int, inbound_plan_id: str
) -> int:
    """Forget a cancelled plan, so the days can be sent to Amazon again.

    Scoped to the matching `inbound_plan_id` rather than clearing whatever the days hold:
    cancelling plan A must not silently detach a shipment created under plan B.

    Only clears rows that have NOT been confirmed. A day carrying a
    `shipment_confirmation_id` describes boxes Amazon is expecting, and forgetting that
    locally would let the same cartons be sent twice.
    """
    result = await db.execute(
        select(ShipmentPackingDay).where(
            ShipmentPackingDay.plan_id == plan_id,
            ShipmentPackingDay.inbound_plan_id == inbound_plan_id,
        )
    )
    cleared = 0
    for day in result.scalars():
        if day.shipment_confirmation_id:
            continue
        day.inbound_plan_id = None
        day.amazon_shipment_id = None
        day.destination_warehouse_id = None
        day.destination_state = None
        cleared += 1
    await db.commit()
    return cleared
