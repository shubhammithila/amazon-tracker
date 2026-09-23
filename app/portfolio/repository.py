"""The only reader and writer of the economics snapshot and the owner's decisions.

Written as SELECT-then-UPDATE-or-INSERT rather than a dialect-specific upsert, so the same code
runs on SQLite locally and PostgreSQL in production — the same reasoning
``app/shipment/repository.py`` documents.

**Two kinds of row live here and the boundary matters.** ``EconomicsDaily`` and ``AdsDaily`` are a
cache of Amazon's numbers: the refresh writes them, nothing edits them, and a wrong value is fixed
by refreshing. ``ProductDecision`` is the owner's own judgement, which Amazon has no opinion about.
Keeping them apart is what lets next month ask "I marked this KILL at -56.8%; what is it now?".

**The cache is keyed per DAY, and every window is a sum over days.** It used to be keyed per
window, which meant a range nobody had fetched had no row to read: ``GET /portfolio`` returned
empty and offered a ~12 minute fetch. The two granularities were measured before the change — a
7-day DAY sum equals the RANGE query to the rupee, and a 7-day DAILY ads report equals SUMMARY
exactly — so summing days is not an approximation of the window figure, it IS the window figure.
``range_completeness`` refuses a range with a missing interior day rather than summing short.

**Every Decimal is cast to float on the way out.** SQLAlchemy returns ``Decimal`` for
``Numeric`` columns and ``JSONResponse`` cannot serialise it. Done here rather than in each
route, because this app has already shipped that exact defect twice — once with datetimes on the
orders payload, once with ``raw_kg`` on the purchasing view — and both were found in a browser
on production rather than by a test.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AdsDaily,
    EconomicsDaily,
    EconomicsRefresh,
    PortfolioSettings,
    Product,
    ProductDecision,
    RatingHistory,
)

logger = logging.getLogger(__name__)

#: How many days of per-day rows to keep. The widest window the tab offers is 90, so an older day
#: can answer no question the screen is able to ask. Purged nightly — the per-window
#: `economics_snapshot` had NO retention and had reached 32 windows / 21,698 rows with one added
#: every night, which is the `ads_performance` growth problem repeating.
DAILY_RETENTION_DAYS = 90

#: How many missing days to NAME when a range cannot be summed. A 90-day range can be short 83
#: days, and the screen needs a sentence rather than a column. The COUNT stays exact — "missing 25
#: days" and "missing 2 days" call for different actions. Same cap, same reason, as
#: `ads.repository.MISSING_DAYS_SHOWN`.
MISSING_DAYS_SHOWN = 5

#: The authoritative grain's `seller_sku`. `""` and not NULL, because SQLite treats NULLs as
#: DISTINCT in a unique index — a nullable column would not stop the same (day, asin) being
#: inserted twice and silently doubling that day, which is what `economics_snapshot`'s index
#: actually permitted.
ASIN_GRAIN = ""

#: What the owner may record. Validated against this rather than stored as free text, so a typo
#: cannot create a fourth category that the dashboard then cannot filter or count.
DECISIONS = ("kill", "keep", "watch")

#: The one settings row's name. A constant because it is written in one place and read in
#: another, and a typo would silently create a second row that nothing reads.
SETTINGS_NAME = "thresholds"


def _float(value) -> float:
    """A float from a Decimal, an int, or None. See the module docstring."""
    return float(value or 0)


def _json(raw) -> dict:
    """A dict from a stored JSON column. Malformed text yields {} rather than raising.

    A single bad row must not blank the dashboard: the figures around it are still worth
    reading, and a hard failure here would be indistinguishable from an outage.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("portfolio: unreadable JSON column, treated as empty")
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ─── Amazon's numbers, cached PER DAY ─────────────────────────────────────────
#
# Keyed per DAY rather than per window, so any sub-range inside the coverage is a GROUP BY rather
# than a ~12 minute fetch. `aggregateBy: { date: DAY }` was measured before this was built: a
# 7-day DAY sum equals the RANGE query to the rupee on sales, ads, net and units, and 30 days is
# 8,010 rows in 25 seconds — the same cost as the RANGE query it replaces. See `EconomicsDaily`.


def expected_days(start: str, end: str) -> list[str]:
    """Every calendar day from ``start`` to ``end`` inclusive, as ISO strings.

    Built from the dates rather than from what is stored, because the question
    `range_completeness` asks is "which days SHOULD be here" — deriving the answer from the rows
    would make any gap invisible by construction.
    """
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except (TypeError, ValueError):
        return []
    if last < first:
        return []
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


async def days_held(db: AsyncSession, *, table=EconomicsDaily) -> set[str]:
    """Which days have rows at all, for the authoritative grain.

    Scoped to `ASIN_GRAIN` deliberately: a day that somehow stored only per-SKU breakdown rows
    holds no total and must not count as present, because `sum_daily` would then return a figure
    missing every product Amazon could not attribute to a single SKU.
    """
    rows = await db.execute(
        select(table.day).where(table.seller_sku == ASIN_GRAIN).distinct()
    )
    return {row[0] for row in rows.all()}


async def range_completeness(db: AsyncSession, start: str, end: str) -> dict:
    """Can this range be summed, and if not, WHICH days are missing?

    ``{"complete": bool, "missing": [day, ...], "missing_count": int,
       "held": [first, last] | None, "days_held": int}``

    **Every DAY, not merely the endpoints.** A missing Tuesday would make the sum quietly
    understate sales, and this dashboard decides which products to kill — an understated total
    looks entirely plausible. Same rule as `ads.repository.range_completeness`, and for the same
    reason: that one exists because a span cannot see an interior gap, which is how a merged
    coverage figure came to promise "summed instantly" for windows the server then refused.

    `missing` is capped at `MISSING_DAYS_SHOWN`; `missing_count` is always exact.
    """
    held = await days_held(db)
    wanted = expected_days(start, end)
    absent = [day for day in wanted if day not in held]
    return {
        "complete": bool(wanted) and not absent,
        "missing": absent[:MISSING_DAYS_SHOWN],
        "missing_count": len(absent),
        "held": [min(held), max(held)] if held else None,
        "days_held": len(held),
    }


async def coverage(db: AsyncSession) -> dict:
    """What the store can answer, for the screen's picker.

    ``{"first": day, "last": day, "days": n}`` — replaces `windows_available`, which listed the
    exact windows previously fetched. The screen used that list to decide whether a range loaded
    instantly, which meant the browser held its own copy of a rule the server also had. That is
    the defect this codebase has shipped three times ("86 orders beside 87 lines"), so the
    instant/not-instant answer now comes from `range_completeness` alone and this is for PROSE.

    **A span cannot see an interior gap**, which is exactly why it gates nothing — the same
    warning `ads.repository.daily_coverage_by_product` carries.
    """
    held = await days_held(db)
    if not held:
        return {"first": None, "last": None, "days": 0}
    return {"first": min(held), "last": max(held), "days": len(held)}


async def latest_window(db: AsyncSession, days: int = 30) -> tuple[str, str] | None:
    """The default range to show: the newest ``days`` days that are actually held.

    Anchored on the newest day held rather than on today, so the tab renders the freshest
    complete picture instead of a range whose last day has not been fetched yet.
    """
    held = await days_held(db)
    if not held:
        return None
    last = max(held)
    first = (date.fromisoformat(last) - timedelta(days=days - 1)).isoformat()
    earliest = min(held)
    return (max(first, earliest), last)


def _rows_to_daily(rows: list[dict], *, sku_grain: bool) -> list[dict]:
    """Amazon's raw rows flattened to per-day column dicts, ready for a bulk insert.

    Parsed through `logic.size_row` — the same function the dashboard uses — so a stored row and a
    rendered row can never disagree about what Amazon said.

    A row with no `startDate` is SKIPPED rather than filed under a guessed day: putting one day's
    sales into another is the kind of error that reconciles to the right total and the wrong
    answer. Same rule as `ads.repository.save_daily`.
    """
    from app.portfolio import logic

    now = datetime.utcnow()
    mapped: dict[tuple[str, str, str], dict] = {}
    for raw in rows:
        day = str(raw.get("startDate") or "")[:10]
        if not day:
            continue
        parsed = logic.size_row(raw, {})
        asin = parsed["asin"]
        if not asin:
            continue
        if sku_grain:
            sku = str(raw.get("msku") or "").strip()
            # No MSKU means the row spans several FNSKUs and belongs to no single channel, so
            # filing it under one would misattribute real money. Amazon's schema allows this.
            if not sku:
                continue
        else:
            sku = ASIN_GRAIN
        # Keyed so a payload that repeats a (day, asin, sku) cannot violate the unique index
        # mid-insert. Amazon has not done this, but a bulk insert fails the whole batch if it does.
        mapped[(day, asin, sku)] = {
            "day": day,
            "child_asin": asin,
            "seller_sku": sku,
            "parent_asin": parsed["parent_asin"] or None,
            "ordered_sales": parsed["sales"],
            "refunded_sales": parsed["refunded"],
            "ad_spend": parsed["ad_spend"],
            "net_proceeds": parsed["net"],
            "units_ordered": parsed["units_ordered"],
            "units_refunded": parsed["units_refunded"],
            "net_units": parsed["units"],
            "fees_json": json.dumps(parsed["fees"]),
            "ads_json": json.dumps(parsed["ad_types"]),
            "fetched_at": now,
        }
    return list(mapped.values())


async def save_economics_daily(
    db: AsyncSession, rows: list[dict], *, sku_grain: bool = False
) -> int:
    """Store per-day economics. **Delete-then-bulk-insert, scoped by (day, grain).**

    The one place in this module that is not the house SELECT-then-UPDATE-or-INSERT, for the
    reason `ads.repository.save_daily` measured: per-row upsert runs at 498 rows/sec against
    30,921 for a bulk insert, which is 6.5 minutes against 6 seconds for 30 days. Nothing is lost
    by replacing rather than merging — a refetch of a day wholly supersedes it, so there is no
    earlier value an upsert would preserve.

    **Scoped by GRAIN as well as by day**, and that is load-bearing: the ASIN-level and per-SKU
    rows share this table, and a delete scoped by day alone would make the MSKU write destroy the
    ASIN totals it had just stored. Exactly the bug `save_daily`'s `(day, ad_product)` scope
    exists to prevent, where deleting by day alone would have wiped 72% of the spend.

    Portable: `delete()` + `insert()` through the ORM, no dialect-specific `ON CONFLICT`.
    """
    mapped = _rows_to_daily(rows, sku_grain=sku_grain)
    if not mapped:
        return 0

    days = {row["day"] for row in mapped}
    grain_filter = (
        EconomicsDaily.seller_sku != ASIN_GRAIN
        if sku_grain
        else EconomicsDaily.seller_sku == ASIN_GRAIN
    )
    await db.execute(
        delete(EconomicsDaily).where(EconomicsDaily.day.in_(sorted(days)), grain_filter)
    )
    await db.execute(insert(EconomicsDaily), mapped)
    await db.commit()
    return len(mapped)


def _summed_amazon_row(key: tuple[str, str], agg: dict, window: tuple[str, str]) -> dict:
    """One summed product back in Amazon's nested shape.

    **The whole point of this function is that `logic.portfolio` needs no changes.** It takes
    Amazon's row shape, so a summed range and a fresh API response are the same input — the
    alternative, a second code path for stored rows, is how a cached dashboard starts disagreeing
    with a freshly-refreshed one.
    """
    asin, sku = key
    return {
        "parentAsin": agg["parent_asin"] or "",
        "childAsin": asin,
        "msku": sku,
        "startDate": window[0],
        "endDate": window[1],
        "sales": {
            "orderedProductSales": {"amount": round(agg["ordered_sales"], 2)},
            "refundedProductSales": {"amount": round(agg["refunded_sales"], 2)},
            "unitsOrdered": agg["units_ordered"],
            "unitsRefunded": agg["units_refunded"],
            "netUnitsSold": agg["net_units"],
        },
        "fees": [
            {"feeTypeName": name,
             "charges": [{"aggregatedDetail": {"totalAmount": {"amount": round(amount, 2)}}}]}
            for name, amount in sorted(agg["fees"].items())
        ],
        "ads": [
            {"adTypeName": name, "charge": {"totalAmount": {"amount": round(amount, 2)}}}
            for name, amount in sorted(agg["ad_types"].items())
        ],
        "netProceeds": {"total": {"amount": round(agg["net_proceeds"], 2)}},
    }


async def _sum_economics(
    db: AsyncSession, window: tuple[str, str], *, sku_grain: bool
) -> list[dict]:
    """Sum the stored days of a range into one row per product, in Amazon's shape."""
    grain_filter = (
        EconomicsDaily.seller_sku != ASIN_GRAIN
        if sku_grain
        else EconomicsDaily.seller_sku == ASIN_GRAIN
    )
    rows = (
        await db.execute(
            select(EconomicsDaily).where(
                EconomicsDaily.day >= window[0],
                EconomicsDaily.day <= window[1],
                grain_filter,
            )
        )
    ).scalars()

    agg: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row.child_asin, row.seller_sku or "")
        bucket = agg.setdefault(key, {
            "parent_asin": row.parent_asin,
            "ordered_sales": 0.0, "refunded_sales": 0.0, "ad_spend": 0.0, "net_proceeds": 0.0,
            "units_ordered": 0, "units_refunded": 0, "net_units": 0,
            "fees": {}, "ad_types": {},
        })
        # First non-empty wins: the parent is a property of the product, not of the day, and a day
        # Amazon reported without one must not blank it.
        if not bucket["parent_asin"] and row.parent_asin:
            bucket["parent_asin"] = row.parent_asin
        bucket["ordered_sales"] += _float(row.ordered_sales)
        bucket["refunded_sales"] += _float(row.refunded_sales)
        bucket["ad_spend"] += _float(row.ad_spend)
        bucket["net_proceeds"] += _float(row.net_proceeds)
        bucket["units_ordered"] += int(row.units_ordered or 0)
        bucket["units_refunded"] += int(row.units_refunded or 0)
        bucket["net_units"] += int(row.net_units or 0)
        # **Fees merge BY NAME, never by position.** Amazon returned 8 distinct fee types here and
        # adds more, so two days can carry different sets — a positional merge would add a removal
        # fee to a referral fee.
        for source, target in ((_json(row.fees_json), "fees"),
                               (_json(row.ads_json), "ad_types")):
            for name, amount in source.items():
                bucket[target][name] = bucket[target].get(name, 0.0) + _float(amount)

    return [_summed_amazon_row(key, value, window) for key, value in agg.items()]


async def load_snapshot(db: AsyncSession, window: tuple[str, str] | None = None) -> list[dict]:
    """The stored economics for a range, summed, in Amazon's own row shape. **ASIN grain only.**

    **`seller_sku == ASIN_GRAIN` is what keeps the totals honest.** The per-SKU breakdown rows live
    in this same table; including them would roughly double every figure on the dashboard, because
    a product's merchant and FBA rows sum to its ASIN row. A test stores both grains and asserts
    the totals are unchanged.

    Every money value is a float here; see the module docstring.
    """
    window = window or await latest_window(db)
    if not window:
        return []
    return await _sum_economics(db, window, sku_grain=False)


async def save_sku_snapshot(db: AsyncSession, rows: list[dict]) -> int:
    """Store the per-SKU (MSKU) economics per day. Kept only for the merchant/FBA split on expand.

    **Never a source of totals** — see `load_snapshot`. Amazon's MSKU grain loses a little to rows
    it cannot attribute to a single SKU, so the ASIN-level rows stay authoritative.
    """
    return await save_economics_daily(db, rows, sku_grain=True)


async def load_sku_snapshot(
    db: AsyncSession, window: tuple[str, str] | None = None
) -> list[dict]:
    """The per-SKU economics for a range, summed. Presentation only."""
    window = window or await latest_window(db)
    if not window:
        return []
    return await _sum_economics(db, window, sku_grain=True)


async def purge_daily(
    db: AsyncSession, *, keep_days: int = DAILY_RETENTION_DAYS, today: date | None = None
) -> int:
    """Delete per-day rows older than the retention window. Returns how many were removed.

    **Not optional housekeeping.** The per-window table this replaces had no retention and grew by
    one window every night; every deploy copies the whole database, so an unbounded table
    eventually breaks the deploy as well as the writes. Runs in the refresh's `finally`, because a
    sweep that only happens on the success path is a side effect rather than a policy.
    """
    cutoff = ((today or date.today()) - timedelta(days=keep_days - 1)).isoformat()
    removed = 0
    for table, label in ((EconomicsDaily, "economics"), (AdsDaily, "ads")):
        result = await db.execute(delete(table).where(table.day < cutoff))
        count = int(result.rowcount or 0)
        removed += count
        if count:
            logger.info("portfolio: purged %d %s daily row(s) before %s", count, label, cutoff)
    await db.commit()
    return removed

# ─── Refresh history ─────────────────────────────────────────────────────────


async def record_refresh(
    db: AsyncSession,
    *,
    window_start: str | None,
    window_end: str | None,
    rows_stored: int,
    error: str | None = None,
    started_at: datetime | None = None,
) -> None:
    """Log one refresh attempt, successful or not.

    A FAILED run is recorded too, which is the point: a dashboard showing four-day-old numbers
    should be able to say "the last three refreshes failed with an auth error" rather than
    merely looking stale.
    """
    db.add(EconomicsRefresh(
        window_start=window_start,
        window_end=window_end,
        rows_stored=rows_stored,
        error=error,
        started_at=started_at or datetime.utcnow(),
        finished_at=datetime.utcnow(),
    ))
    await db.commit()


async def last_refresh(db: AsyncSession) -> dict | None:
    """The newest refresh attempt, JSON-safe, or None if it has never run."""
    row = (
        await db.execute(
            select(EconomicsRefresh)
            .order_by(EconomicsRefresh.started_at.desc(), EconomicsRefresh.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "window_start": row.window_start,
        "window_end": row.window_end,
        "rows_stored": int(row.rows_stored or 0),
        "error": row.error or "",
        # isoformat here, not in the route: a datetime reaching JSONResponse is a 500.
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


# ─── Ratings, from our own scraper ────────────────────────────────────────────


async def load_ratings(db: AsyncSession) -> dict[str, dict]:
    """`{asin: {rating, rating_count, scraped_at}}` — the newest rating per product.

    **ONE query for every product.** The tab this replaced ran two queries per ASIN inside a
    loop: at 262 products that is 524 round trips to render one page, and it grew with the
    catalogue. Here a single grouped subquery finds each product's latest ``scraped_at`` and one
    join fetches those rows, which the existing ``idx_rating_history_product_date`` index
    already serves.

    ``scraped_at`` travels with the value because the dashboard has to be able to say the
    ratings are stale — they were nine days old when this was written, and a nine-day-old star
    rating silently averaged into a kill decision is exactly the kind of thing that should be
    on screen instead.
    """
    newest = (
        select(
            RatingHistory.product_id.label("product_id"),
            func.max(RatingHistory.scraped_at).label("scraped_at"),
        )
        .group_by(RatingHistory.product_id)
        .subquery()
    )

    rows = await db.execute(
        select(
            Product.asin,
            RatingHistory.rating,
            RatingHistory.rating_count,
            RatingHistory.scraped_at,
        )
        .join(RatingHistory, RatingHistory.product_id == Product.id)
        .join(
            newest,
            (newest.c.product_id == RatingHistory.product_id)
            & (newest.c.scraped_at == RatingHistory.scraped_at),
        )
    )

    out: dict[str, dict] = {}
    for asin, rating, count, scraped_at in rows.all():
        key = (asin or "").strip().upper()
        if not key:
            continue
        out[key] = {
            # float, not Decimal: Numeric(2, 1) would otherwise reach JSONResponse.
            "rating": float(rating) if rating is not None else None,
            "rating_count": int(count or 0),
            "scraped_at": scraped_at.isoformat() if scraped_at else None,
        }
    return out


# ─── The owner's decisions: ours, not Amazon's ────────────────────────────────


async def load_decisions(db: AsyncSession) -> dict[str, dict]:
    """`{parent_asin: {decision, note, decided_at, decided_by}}`.

    A dict because every caller looks up one parent while rendering its row.
    """
    rows = (await db.execute(select(ProductDecision))).scalars()
    return {
        row.parent_asin: {
            "decision": row.decision,
            "note": row.note or "",
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "decided_by": row.decided_by or "",
            "snapshot": _json(row.snapshot_json),
        }
        for row in rows
    }


async def save_decision(
    db: AsyncSession,
    parent_asin: str,
    decision: str,
    *,
    note: str = "",
    snapshot: dict | None = None,
    decided_by: str = "",
) -> dict[str, dict]:
    """Record or clear one product's decision. Returns the full map afterwards.

    An empty ``decision`` DELETES the row, so absence is the single representation of "not
    decided" — no stale note can sit behind a cleared flag. The same rule the per-order packed
    tick follows, and the opposite of raw stock, where a stored 0 is itself a fact.

    ``snapshot`` records the figures at the moment of the decision. Without it, revisiting a
    kill in three months means trusting memory about what the margin was — and the whole point
    of keeping decisions is to be able to check whether they worked.

    The full map is returned rather than nothing, so the screen re-renders from committed truth
    instead of from what it believes it saved.
    """
    parent_asin = (parent_asin or "").strip().upper()
    if not parent_asin:
        return await load_decisions(db)

    row = (
        await db.execute(
            select(ProductDecision).where(ProductDecision.parent_asin == parent_asin)
        )
    ).scalar_one_or_none()

    if not decision:
        if row is not None:
            await db.delete(row)
            await db.commit()
        return await load_decisions(db)

    if row is None:
        row = ProductDecision(parent_asin=parent_asin)
        db.add(row)
    row.decision = decision
    row.note = note or ""
    row.decided_by = decided_by or ""
    row.decided_at = datetime.utcnow()
    if snapshot is not None:
        row.snapshot_json = json.dumps(snapshot)

    await db.commit()
    return await load_decisions(db)


# ─── Advertising: cost against ATTRIBUTED sales ───────────────────────────────
#
# A separate table from the economics because it comes from a separate API with its own failure
# mode: the ad report takes 15-25 minutes to generate against the economics query's 30 seconds, so
# the two are fetched in separate phases and an ads failure must not be able to cost the margins.


async def save_ads_daily(db: AsyncSession, rows: list[dict]) -> int:
    """Store per-day, per-SKU ad cost and attributed sales. Delete-then-bulk-insert, scoped by day.

    `rows` is what `ads.fetch_acos(daily=True)` returns — already aggregated to one row per
    (day, asin, sku), so this does no summing of its own. Two grains in one table is what the
    aggregation in `ads.py` exists to prevent.

    A row with no `day` is SKIPPED rather than filed under a guessed date: `timeUnit: DAILY` is
    accepted by Amazon WITHOUT the `date` column (measured), and those rows carry no date at all,
    so a default would put one day's spend into another.
    """
    if not rows:
        return 0

    now = datetime.utcnow()
    mapped: dict[tuple[str, str, str], dict] = {}
    for raw in rows:
        day = str(raw.get("day") or "")[:10]
        asin = str(raw.get("child_asin") or "").strip().upper()
        if not day or not asin:
            continue
        sku = str(raw.get("seller_sku") or "").strip()
        mapped[(day, asin, sku)] = {
            "day": day,
            "child_asin": asin,
            "seller_sku": sku,
            "cost": raw.get("cost") or 0,
            "attributed_sales": raw.get("attributed_sales") or 0,
            "purchases": int(raw.get("purchases") or 0),
            "clicks": int(raw.get("clicks") or 0),
            "impressions": int(raw.get("impressions") or 0),
            "fetched_at": now,
        }
    if not mapped:
        return 0

    days = {row["day"] for row in mapped.values()}
    await db.execute(delete(AdsDaily).where(AdsDaily.day.in_(sorted(days))))
    await db.execute(insert(AdsDaily), list(mapped.values()))
    await db.commit()
    return len(mapped)


async def load_ads_snapshot(
    db: AsyncSession, window: tuple[str, str] | None = None
) -> tuple[dict, dict]:
    """`(by_asin, by_sku)` ad figures summed over a range. Both empty when nothing is held.

    Two shapes because two consumers need different grains and each would otherwise re-derive
    the other: `size_row` wants one figure per ASIN (the dashboard's row), and `channel_split`
    wants per (asin, sku) so it can attribute spend to merchant or FBA. Rolling up here means the
    two cannot disagree about a total.

    **Summing days is exact here, which was the open question.** `attributedSalesSameSku14d` can
    credit a sale up to 14 days after the click, so the fear was that one-day rows would smear
    across boundaries the way CHUNKED reports do. Measured on 7 real days: DAILY and SUMMARY
    returned identical cost (3,47,570.00) and identical attributed sales (3,81,534.93), because
    Amazon attributes each sale back to the CLICK's day. See `AdsDaily`.

    Floats throughout — `Numeric` returns `Decimal`, which `JSONResponse` cannot serialise.
    """
    window = window or await latest_window(db)
    if not window:
        return {}, {}

    rows = (
        await db.execute(
            select(AdsDaily).where(
                AdsDaily.day >= window[0],
                AdsDaily.day <= window[1],
            )
        )
    ).scalars()

    by_sku: dict[tuple, dict] = {}
    by_asin: dict[str, dict] = {}

    def _bucket(store, key):
        return store.setdefault(key, {
            "cost": 0.0, "attributed_sales": 0.0, "purchases": 0, "clicks": 0, "impressions": 0,
        })

    for row in rows:
        figures = {
            "cost": _float(row.cost),
            "attributed_sales": _float(row.attributed_sales),
            "purchases": int(row.purchases or 0),
            "clicks": int(row.clicks or 0),
            "impressions": int(row.impressions or 0),
        }
        # Both grains ACCUMULATE now, where the per-window version could assign: several days
        # carry the same (asin, sku), so an assignment would keep only the last day's spend.
        for store, key in ((by_sku, (row.child_asin, row.seller_sku or "")),
                           (by_asin, row.child_asin)):
            bucket = _bucket(store, key)
            for money in ("cost", "attributed_sales"):
                bucket[money] = round(bucket[money] + figures[money], 2)
            for count in ("purchases", "clicks", "impressions"):
                bucket[count] += figures[count]
    return by_asin, by_sku


# ─── The owner's editable verdict thresholds ──────────────────────────────────


async def load_settings(db: AsyncSession) -> dict:
    """The saved thresholds, merged over the measured defaults.

    Always returns a COMPLETE set: a partially-saved row must not take the dashboard down, and
    `logic.thresholds_or_default` fills anything absent.
    """
    from app.portfolio import logic

    row = (
        await db.execute(
            select(PortfolioSettings).where(PortfolioSettings.name == SETTINGS_NAME)
        )
    ).scalar_one_or_none()
    return logic.thresholds_or_default(_json(row.value_json) if row else {})


async def save_settings(db: AsyncSession, values: dict, *, updated_by: str = "") -> dict:
    """Store edited thresholds. Returns the complete effective set.

    **Unknown keys are REFUSED by the caller, not silently dropped here** — the route validates
    against `logic.DEFAULT_THRESHOLDS` so a typo produces a 400 rather than an edit that appears
    to work and changes nothing. This function stores only recognised keys as a second guard.

    An empty dict RESETS to the measured defaults by deleting the row, so "reset" and "never
    edited" are the same state rather than two — the same reasoning as clearing a decision.
    """
    from app.portfolio import logic

    row = (
        await db.execute(
            select(PortfolioSettings).where(PortfolioSettings.name == SETTINGS_NAME)
        )
    ).scalar_one_or_none()

    cleaned = {
        key: float(value)
        for key, value in (values or {}).items()
        if key in logic.DEFAULT_THRESHOLDS and value is not None
    }

    if not cleaned:
        if row is not None:
            await db.delete(row)
            await db.commit()
        return logic.thresholds_or_default({})

    if row is None:
        row = PortfolioSettings(name=SETTINGS_NAME)
        db.add(row)
    row.value_json = json.dumps(cleaned)
    row.updated_by = updated_by or ""
    row.updated_at = datetime.utcnow()
    await db.commit()
    return logic.thresholds_or_default(cleaned)
