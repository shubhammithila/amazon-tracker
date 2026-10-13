"""Fetch -> guard -> store, one at a time.

`finally` clears the flag: a cancelled task must not leave every later refresh refused for the
life of the process (the Portfolio refresh learned that from a mutation).
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.database import async_session
from app.inventory import fetch, logic, repository
from app.models import FbaInventoryRefresh, FbaInventoryRow

logger = logging.getLogger(__name__)
STATE = {"running": False}


async def run(source: str, *, get=None) -> dict:
    if STATE["running"]:
        return {"refused": True}
    STATE["running"] = True
    try:
        async with async_session() as db:
            run_row = FbaInventoryRefresh(source=source, status="running", started_at=datetime.utcnow())
            db.add(run_row)
            await db.commit()
            try:
                rows = logic.reduce_summaries(await fetch.fetch_summaries(get=get))
                if logic.looks_empty(rows):
                    run_row.status = "refused"
                    run_row.error = (f"Amazon listed {len(rows)} FBA SKUs and every one at zero, so the "
                                     "answer was not used; the previous stock reading stands.")
                else:
                    for r in rows:
                        db.add(FbaInventoryRow(refresh_id=run_row.id, seller_sku=r["seller_sku"],
                                               asin=r["asin"], fnsku=r["fnsku"],
                                               amazon_updated_at=r["amazon_updated_at"],
                                               **{k: r[k] for k in logic.QUANTITY_FIELDS}))
                    run_row.status = "done"
                run_row.sku_count = len(rows)
                run_row.units = sum(r["held"] for r in rows)
            except Exception as exc:  # noqa: BLE001 - recorded on the run and shown on screen
                logger.warning("FBA inventory read failed: %s", exc)
                run_row.status = "failed"
                run_row.error = str(exc)[:500] or exc.__class__.__name__
            run_row.finished_at = datetime.utcnow()
            await db.flush()
            await repository.purge(db)
            await db.commit()
            return {"id": run_row.id, "status": run_row.status, "sku_count": run_row.sku_count,
                    "units": run_row.units, "error": run_row.error}
    finally:
        STATE["running"] = False
