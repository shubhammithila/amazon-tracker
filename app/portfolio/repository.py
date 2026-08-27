"""The only reader and writer of the economics snapshot and the owner's decisions.

Written as SELECT-then-UPDATE-or-INSERT rather than a dialect-specific upsert, so the same code
runs on SQLite locally and PostgreSQL in production — the same reasoning
``app/shipment/repository.py`` documents.

**Two kinds of row live here and the boundary matters.** ``EconomicsSnapshot`` is a cache of
Amazon's numbers: the refresh writes it, nothing edits it, and a wrong value is fixed by
refreshing. ``ProductDecision`` is the owner's own judgement, which Amazon has no opinion about.
Keeping them apart is what lets next month ask "I marked this KILL at -56.8%; what is it now?".

**Every Decimal is cast to float on the way out.** SQLAlchemy returns ``Decimal`` for
``Numeric`` columns and ``JSONResponse`` cannot serialise it. Done here rather than in each
route, because this app has already shipped that exact defect twice — once with datetimes on the
orders payload, once with ``raw_kg`` on the purchasing view — and both were found in a browser
on production rather than by a test.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EconomicsRefresh,
    EconomicsSnapshot,
    Product,
    ProductDecision,
    RatingHistory,
)

logger = logging.getLogger(__name__)

#: What the owner may record. Validated against this rather than stored as free text, so a typo
#: cannot create a fourth category that the dashboard then cannot filter or count.
DECISIONS = ("kill", "keep", "watch")


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


# ─── The economics snapshot: Amazon's numbers, cached ─────────────────────────


async def save_snapshot(
    db: AsyncSession, window_start: str, window_end: str, rows: list[dict]
) -> int:
    """Store one window's economics. Returns how many rows were written.

    Takes Amazon's raw row shape (as ``economics.fetch_economics`` returns it) and flattens it
    into columns, so the parsing lives in ONE place rather than in every reader.

    Upserts on (window_start, window_end, child_asin): pressing Refresh twice in a morning must
    correct the figures, not double the portfolio.
    """
    if not rows:
        return 0

    from app.portfolio import logic

    incoming: dict[str, dict] = {}
    for raw in rows:
        # Reuse the same parser the dashboard uses, so a stored row and a rendered row can
        # never disagree about what Amazon said.
        parsed = logic.size_row(raw, {})
        if parsed["asin"]:
            incoming[parsed["asin"]] = parsed

    existing = {
        row.child_asin: row
        for row in (
            await db.execute(
                select(EconomicsSnapshot).where(
                    EconomicsSnapshot.window_start == window_start,
                    EconomicsSnapshot.window_end == window_end,
                    EconomicsSnapshot.child_asin.in_(sorted(incoming)),
                )
            )
        ).scalars()
    }

    now = datetime.utcnow()
    for asin, parsed in incoming.items():
        row = existing.get(asin)
        if row is None:
            row = EconomicsSnapshot(
                window_start=window_start, window_end=window_end, child_asin=asin
            )
            db.add(row)
        row.parent_asin = parsed["parent_asin"] or None
        row.ordered_sales = parsed["sales"]
        row.refunded_sales = parsed["refunded"]
        row.ad_spend = parsed["ad_spend"]
        row.net_proceeds = parsed["net"]
        row.units_ordered = parsed["units_ordered"]
        row.units_refunded = parsed["units_refunded"]
        row.net_units = parsed["units"]
        row.fees_json = json.dumps(parsed["fees"])
        row.ads_json = json.dumps(parsed["ad_types"])
        row.fetched_at = now

    await db.commit()
    return len(incoming)


async def latest_window(db: AsyncSession) -> tuple[str, str] | None:
    """The most recent (start, end) held, or None when nothing has been fetched.

    Ordered by ``window_end`` DESC rather than by ``fetched_at``: a re-run of an OLDER window
    would otherwise become "the latest" and quietly shift the whole dashboard backwards in
    time.
    """
    row = (
        await db.execute(
            select(EconomicsSnapshot.window_start, EconomicsSnapshot.window_end)
            .order_by(EconomicsSnapshot.window_end.desc(), EconomicsSnapshot.id.desc())
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def load_snapshot(db: AsyncSession, window: tuple[str, str] | None = None) -> list[dict]:
    """The stored economics for one window, in Amazon's own row shape.

    **Returns Amazon's nested shape, not the flat columns**, so ``logic.portfolio`` has exactly
    one input format whether the rows came from the API a second ago or from the database. The
    alternative — a second code path for stored rows — is how a cached dashboard starts
    disagreeing with a freshly-refreshed one.

    Every money value is a float here; see the module docstring.
    """
    window = window or await latest_window(db)
    if not window:
        return []

    rows = (
        await db.execute(
            select(EconomicsSnapshot).where(
                EconomicsSnapshot.window_start == window[0],
                EconomicsSnapshot.window_end == window[1],
            )
        )
    ).scalars()

    out = []
    for row in rows:
        fees = _json(row.fees_json)
        ads = _json(row.ads_json)
        out.append({
            "parentAsin": row.parent_asin or "",
            "childAsin": row.child_asin,
            "startDate": row.window_start,
            "endDate": row.window_end,
            "sales": {
                "orderedProductSales": {"amount": _float(row.ordered_sales)},
                "refundedProductSales": {"amount": _float(row.refunded_sales)},
                "unitsOrdered": int(row.units_ordered or 0),
                "unitsRefunded": int(row.units_refunded or 0),
                "netUnitsSold": int(row.net_units or 0),
            },
            "fees": [
                {"feeTypeName": name,
                 "charges": [{"aggregatedDetail": {"totalAmount": {"amount": _float(amount)}}}]}
                for name, amount in fees.items()
            ],
            "ads": [
                {"adTypeName": name, "charge": {"totalAmount": {"amount": _float(amount)}}}
                for name, amount in ads.items()
            ],
            "netProceeds": {"total": {"amount": _float(row.net_proceeds)}},
        })
    return out


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
