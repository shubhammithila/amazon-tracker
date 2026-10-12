"""The only reader and writer of `projection_row` and the Projections settings.

Since 12 Oct 2026 a row holds only the owner's per-product inputs (seasonal and four lead times).
Sales are never stored here: they are read live from the Profit tab's per-day economics every time.
The older columns (`last_month_sale`, `daily_rate`, `wh_buffer_days`, `sales_source`, ...) stay in
the table unread rather than being dropped, so nothing typed before is destroyed.

**A GET never writes.** A product with no stored row is shown with defaults computed on the fly,
and a row is written only when the owner saves an edit. Writing on read let two page loads race to
insert the same product.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSettings, ProjectionRow
from app.projections import logic

SETTING_NAME = "projection_blend"
_FIELDS = ("id", "product_key", "parent_product", "brand") + logic.EDITABLE


def _row_to_dict(row: ProjectionRow) -> dict:
    out = {field: getattr(row, field) for field in _FIELDS}
    if out["seasonal_impact"] is not None:
        out["seasonal_impact"] = float(out["seasonal_impact"])
    out["saved_at"] = row.updated_at.isoformat() if row.updated_at else None
    return out


async def load_rows(db: AsyncSession) -> list[dict]:
    """Every stored config row, excluded or not: the MRP sheet's Active flag decides what is shown
    now, the way the Profit tab does, so an old "removed" mark no longer hides anything."""
    return [_row_to_dict(r) for r in (await db.execute(select(ProjectionRow))).scalars().all()]


async def save_config(db: AsyncSession, key: str, name: str, brand: str, values: dict,
                      *, row_id: int | None = None, updated_by: str = "") -> dict:
    """Store one product's edited inputs. `row_id` is a pre-key row matched by name, which this
    save adopts (stamping its key) so its earlier lead times are not orphaned."""
    row = (await db.execute(select(ProjectionRow).where(ProjectionRow.product_key == key))).scalar_one_or_none()
    if row is None and row_id is not None:
        row = await db.get(ProjectionRow, row_id)
    if row is None:
        row = ProjectionRow(parent_product=name, sales_source="sheet")
        db.add(row)
    row.product_key = key
    row.parent_product = name
    row.brand = brand
    for field in logic.EDITABLE:
        if field in values:
            setattr(row, field, values[field])
    row.needs_review = False
    row.updated_at = datetime.utcnow()
    row.updated_by = updated_by or None
    await db.commit()
    await db.refresh(row)
    return _row_to_dict(row)


async def _settings_row(db: AsyncSession):
    return (await db.execute(select(PortfolioSettings)
                             .where(PortfolioSettings.name == SETTING_NAME))).scalar_one_or_none()


async def load_settings(db: AsyncSession) -> dict:
    row = await _settings_row(db)
    stored = json.loads(row.value_json) if row and row.value_json else {}
    return logic.settings_or_default(stored)


async def save_settings(db: AsyncSession, values: dict, *, updated_by: str = "") -> dict:
    """Merge `values` over what is stored. Validated by the caller; checked again on read."""
    current = await load_settings(db)
    current.update({k: float(v) for k, v in values.items()})
    row = await _settings_row(db)
    if row is None:
        row = PortfolioSettings(name=SETTING_NAME)
        db.add(row)
    row.value_json = json.dumps(current)
    if hasattr(row, "updated_by"):
        row.updated_by = updated_by or None
    if hasattr(row, "updated_at"):
        row.updated_at = datetime.utcnow()
    await db.commit()
    return logic.settings_or_default(current)
