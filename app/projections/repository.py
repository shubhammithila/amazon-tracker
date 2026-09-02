"""The only reader and writer of `projection_row` and `projection_refresh`.

SELECT-then-UPDATE-or-INSERT throughout, the same dialect-neutral idiom
`app.orders.repository.save_raw_stock` and `app.shipment.repository` document — one code path
runs identically on SQLite locally and PostgreSQL in production.

**Every Decimal is cast to float on the way out.** SQLAlchemy returns `Decimal` for `Numeric`
columns and `JSONResponse` cannot serialise it. This app has already shipped that exact defect
twice (orders payload datetimes, then `raw_kg`), both found in a browser on production — done
once here so every route inherits the fix.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSettings, ProjectionRefresh, ProjectionRow
from app.projections import logic

logger = logging.getLogger(__name__)

#: `PortfolioSettings.name` for the blend weight/threshold. That table is shared and name-keyed
#: across features — Portfolio's own verdict thresholds live under `"thresholds"`, and the Ads
#: guardrails live under `GUARDRAIL_SETTING_NAME` — so this follows the same pattern under its
#: own name rather than reusing either feature's specific load/save functions.
BLEND_SETTING_NAME = "projection_blend"

#: Every column on `ProjectionRow` a caller may read or write, EXCLUDING the primary key and the
#: audit columns (`updated_at`, `updated_by`) — kept as one tuple so `_row_to_dict` and
#: `save_row`'s `setattr` loop cannot drift about which fields exist.
_FIELDS = (
    "parent_product", "brand", "purchase_rate", "supplier_to_wh", "packing", "wh_to_ixd",
    "ixd_to_fba", "wh_buffer_days", "seasonal_impact", "growth_rate", "needs_review",
    "sales_source", "last_month_sale", "seven_day_rate", "thirty_day_rate", "daily_rate",
    "diverged", "current_fba_stock", "current_wh_stock",
)

#: Which of the fields above are Decimal-backed and need the float conversion on the way out.
_NUMERIC_FIELDS = (
    "purchase_rate", "wh_buffer_days", "seasonal_impact", "growth_rate", "last_month_sale",
    "seven_day_rate", "thirty_day_rate", "daily_rate", "current_fba_stock", "current_wh_stock",
)


def _row_to_dict(row: ProjectionRow) -> dict:
    out = {}
    for field in _FIELDS:
        value = getattr(row, field)
        out[field] = float(value) if field in _NUMERIC_FIELDS and value is not None else value
    return out


async def load_rows(db: AsyncSession) -> list[dict]:
    """Every stored parent row, as plain dicts with floats — never `Decimal`."""
    rows = (await db.execute(select(ProjectionRow))).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def save_row(
    db: AsyncSession, parent_product: str, values: dict, *, source: str, updated_by: str = "",
) -> dict:
    """Upsert one parent's row. `source` is stamped on EVERY call — there is no field left
    unset, so a caller cannot accidentally leave a row's provenance stale.
    """
    existing = (
        await db.execute(select(ProjectionRow).where(ProjectionRow.parent_product == parent_product))
    ).scalar_one_or_none()

    if existing is None:
        existing = ProjectionRow(parent_product=parent_product)
        db.add(existing)

    for key, value in values.items():
        if key in _FIELDS and key != "parent_product":
            setattr(existing, key, value)
    existing.sales_source = source
    existing.updated_at = datetime.utcnow()
    existing.updated_by = updated_by or existing.updated_by

    await db.commit()
    await db.refresh(existing)
    return _row_to_dict(existing)


async def upsert_sheet_rows(db: AsyncSession, rows: list[dict]) -> int:
    """Bulk-write computed rows from the weekly refresh. Returns how many rows were actually
    updated — **a row whose stored `sales_source == "manual"` is SKIPPED and not counted**, which
    is the entire mechanism behind "a manual override survives a refresh". A brand-new parent
    (no existing row at all) is created with `sales_source="sheet"`.
    """
    if not rows:
        return 0

    names = [r["parent_product"] for r in rows]
    existing = {
        row.parent_product: row
        for row in (
            await db.execute(select(ProjectionRow).where(ProjectionRow.parent_product.in_(names)))
        ).scalars()
    }

    written = 0
    now = datetime.utcnow()
    for incoming in rows:
        name = incoming["parent_product"]
        current = existing.get(name)
        if current is not None and current.sales_source == "manual":
            continue
        if current is None:
            current = ProjectionRow(parent_product=name)
            db.add(current)
        for key, value in incoming.items():
            if key in _FIELDS and key != "parent_product":
                setattr(current, key, value)
        current.sales_source = "sheet"
        current.updated_at = now
        written += 1

    await db.commit()
    return written


async def reset_to_sheet(db: AsyncSession, parent_product: str) -> dict | None:
    """Clear a manual override, so the next scheduled recompute (or a manual "Refresh now")
    updates it again. `None` if no row exists for that name — nothing to reset.
    """
    row = (
        await db.execute(select(ProjectionRow).where(ProjectionRow.parent_product == parent_product))
    ).scalar_one_or_none()
    if row is None:
        return None
    row.sales_source = "sheet"
    await db.commit()
    await db.refresh(row)
    return _row_to_dict(row)


# ─── Blend settings ────────────────────────────────────────────────────────────


async def load_blend_settings(db: AsyncSession) -> dict:
    """The saved blend weight/threshold, merged over the measured defaults. Range-checked on
    the way out — see `app.projections.logic.blend_or_default`."""
    row = (
        await db.execute(select(PortfolioSettings).where(PortfolioSettings.name == BLEND_SETTING_NAME))
    ).scalar_one_or_none()
    stored = {}
    if row and row.value_json:
        try:
            stored = json.loads(row.value_json) or {}
        except json.JSONDecodeError:
            logger.warning("projections: stored blend settings are not valid JSON; using defaults")
    return logic.blend_or_default(stored)


async def save_blend_settings(db: AsyncSession, values: dict, *, updated_by: str = "") -> dict:
    """Validate and store the blend settings. Raises `ValueError` naming the first problem —
    the same shape as `app.ads.repository.save_guardrails`."""
    for key, value in (values or {}).items():
        problem = logic.blend_setting_error(key, value)
        if problem:
            raise ValueError(problem)

    merged = logic.blend_or_default(values)
    row = (
        await db.execute(select(PortfolioSettings).where(PortfolioSettings.name == BLEND_SETTING_NAME))
    ).scalar_one_or_none()
    if row:
        row.value_json = json.dumps(merged)
        row.updated_by = updated_by or row.updated_by
    else:
        db.add(PortfolioSettings(
            name=BLEND_SETTING_NAME, value_json=json.dumps(merged), updated_by=updated_by,
        ))
    await db.commit()
    return merged


async def reset_blend_settings(db: AsyncSession) -> dict:
    """Delete the stored row so the measured defaults apply again."""
    row = (
        await db.execute(select(PortfolioSettings).where(PortfolioSettings.name == BLEND_SETTING_NAME))
    ).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return dict(logic.DEFAULT_BLEND)


# ─── Refresh history ───────────────────────────────────────────────────────────


async def record_refresh(
    db: AsyncSession, *, window_start: str | None, window_end: str | None, rows_stored: int,
    error: str | None = None, started_at: datetime | None = None,
) -> None:
    """Log one refresh attempt, successful or not — the same shape as
    `app.portfolio.repository.record_refresh`."""
    db.add(ProjectionRefresh(
        window_start=window_start, window_end=window_end, rows_stored=rows_stored, error=error,
        started_at=started_at or datetime.utcnow(), finished_at=datetime.utcnow(),
    ))
    await db.commit()


async def last_refresh(db: AsyncSession) -> dict | None:
    """The newest refresh attempt, JSON-safe, or `None` if it has never run."""
    row = (
        await db.execute(
            select(ProjectionRefresh)
            .order_by(ProjectionRefresh.started_at.desc(), ProjectionRefresh.id.desc())
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
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
