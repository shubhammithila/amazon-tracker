"""Runs of the FBA Inventory read. Only a `done` run is current stock."""
from __future__ import annotations

from sqlalchemy import delete, select

from app.inventory.logic import HELD_FIELDS, QUANTITY_FIELDS
from app.models import FbaInventoryRefresh, FbaInventoryRow

KEEP_RUNS = 30


async def latest_good(db):
    return (await db.execute(select(FbaInventoryRefresh).where(FbaInventoryRefresh.status == "done")
                             .order_by(FbaInventoryRefresh.id.desc()).limit(1))).scalar()


async def latest(db):
    """The newest FINISHED run, whatever its outcome, so a failure can be reported."""
    return (await db.execute(select(FbaInventoryRefresh).where(FbaInventoryRefresh.status != "running")
                             .order_by(FbaInventoryRefresh.id.desc()).limit(1))).scalar()


async def rows_for(db, refresh_id: int) -> list[dict]:
    rows = (await db.execute(select(FbaInventoryRow).where(FbaInventoryRow.refresh_id == refresh_id)
                             .order_by(FbaInventoryRow.seller_sku))).scalars().all()
    out = []
    for r in rows:
        d = {"seller_sku": r.seller_sku, "asin": r.asin,
             **{k: getattr(r, k) or 0 for k in QUANTITY_FIELDS}}
        d["held"] = sum(d[k] for k in HELD_FIELDS)
        out.append(d)
    return out


async def purge(db, keep: int = KEEP_RUNS) -> int:
    ids = (await db.execute(select(FbaInventoryRefresh.id).order_by(FbaInventoryRefresh.id.desc())
                            .offset(keep))).scalars().all()
    if ids:
        await db.execute(delete(FbaInventoryRow).where(FbaInventoryRow.refresh_id.in_(ids)))
        await db.execute(delete(FbaInventoryRefresh).where(FbaInventoryRefresh.id.in_(ids)))
    return len(ids)
