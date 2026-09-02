"""The weekly 7d/30d sales recompute. **No new Amazon integration** — reuses
`app.portfolio.economics.fetch_economics` and `app.portfolio.repository.save_snapshot`/
`load_snapshot`/`windows_available` exactly as the Portfolio tab's own nightly refresh does.

**A failed or partial fetch must not overwrite good data.** Every parent's existing row is left
untouched if either window's fetch raises — the same discipline `app.ads.refresh`'s
`ads_refresh` table enforces: a record of the failure is kept (`repository.record_refresh`), but
nothing already stored is silently replaced with a wrong number.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import ist
from app.portfolio import economics
from app.portfolio import repository as portfolio_repository
from app.projections import logic, repository
from app.shipment import catalogue
from app.shipment.spapi import SpApiError

logger = logging.getLogger(__name__)


async def _ensure_window(
    db: AsyncSession, days: int, *, sleep, today: date,
) -> tuple[str, str]:
    """Ensure an economics window is stored, and return `(start, end)`. **Checks the cache
    BEFORE fetching, not after** — the whole reason `windows_available` is consulted at all is
    to avoid a ~2-minute Data Kiosk query for a window the Portfolio tab's own nightly refresh
    (or a previous run of this job) already stored. Calling `fetch_economics` unconditionally
    and only skipping the SAVE would still pay the fetch cost every time, which defeats the
    entire point of sharing the cache with the Portfolio tab.

    `economics.window_for(today, days)` is the same pure calculation `fetch_economics` uses
    internally to turn "the last N days" into concrete dates — calling it here costs nothing and
    is what makes the cache check possible before any Amazon call. `today` is a REQUIRED
    parameter, not a default of `date.today()`, so a test can pin it and exercise the cache-hit
    path deterministically rather than depending on which real-world date the suite happens to
    run on.
    """
    start, end = economics.window_for(today, days)
    cached = await portfolio_repository.windows_available(db, limit=50)
    if any(w["start"] == start and w["end"] == end for w in cached):
        return start, end

    asin_rows, _sku_rows, start, end = await economics.fetch_economics(
        days=days, sleep=sleep, today=today,
    )
    await portfolio_repository.save_snapshot(db, start, end, asin_rows)
    return start, end


async def run(db: AsyncSession, *, sleep=asyncio.sleep, today: date | None = None) -> dict:
    """Recompute every sheet-sourced parent row's blended daily rate. Returns
    `{"rows_stored": int, "error": str | None, "window_start": str, "window_end": str}`.

    **Checks `windows_available` before fetching**, so a 30-day window the Portfolio tab's own
    nightly refresh already stored costs nothing extra here — the two features share one cache.

    `today` defaults to the IST calendar day (`app.ist.today()`), never the server's raw UTC
    `date.today()` — this codebase has shipped the IST/UTC boundary bug six separate times (see
    `app/ist.py`'s own docstring), and "which day's window is this" is exactly the kind of
    decision that bug class breaks. A caller (a test) may pin it explicitly.
    """
    if today is None:
        today = ist.today()
    started = datetime.utcnow()
    try:
        sheet_products, _warning, _source = await catalogue.load_catalogue()
        groups = logic.group_active_by_name(sheet_products)

        thirty_start, thirty_end = await _ensure_window(db, 30, sleep=sleep, today=today)
        seven_start, seven_end = await _ensure_window(db, 7, sleep=sleep, today=today)

        thirty_rows = await portfolio_repository.load_snapshot(db, (thirty_start, thirty_end))
        seven_rows = await portfolio_repository.load_snapshot(db, (seven_start, seven_end))

        kg_30 = logic.sales_kg_by_parent(thirty_rows, groups)
        kg_7 = logic.sales_kg_by_parent(seven_rows, groups)

        blend = await repository.load_blend_settings(db)
        weight = blend["seven_day_weight"]
        divergence_fraction = blend["divergence_pct"] / 100

        to_write = []
        for name in groups:
            thirty_kg = kg_30.get(name, 0.0)
            # `name in kg_7` is the only check that matters: `sales_kg_by_parent` returns an
            # entry for a parent the moment ANY of its ASINs has an economics row in that
            # window — including a row reporting 0 units — so "absent from kg_7" means no
            # snapshot row exists at all (pass None) and "present with value 0.0" means a
            # genuine zero-sales week (pass 0.0). See logic.blended_daily_rate's own docstring
            # for why the two must stay distinguishable.
            seven_kg = kg_7[name] if name in kg_7 else None

            rate, diverged = logic.blended_daily_rate(
                thirty_kg, seven_kg, weight, divergence_fraction=divergence_fraction,
            )
            to_write.append({
                "parent_product": name,
                "thirty_day_rate": round(thirty_kg / 30, 2),
                "seven_day_rate": None if seven_kg is None else round(seven_kg / 7, 2),
                "daily_rate": rate,
                "diverged": diverged,
                "last_month_sale": round(thirty_kg, 2),
            })

        written = await repository.upsert_sheet_rows(db, to_write)
        await repository.record_refresh(
            db, window_start=thirty_start, window_end=thirty_end, rows_stored=written,
            started_at=started,
        )
        return {"rows_stored": written, "error": None,
                "window_start": thirty_start, "window_end": thirty_end}

    except SpApiError as exc:
        logger.warning("projections refresh failed: %s", exc)
        await repository.record_refresh(
            db, window_start=None, window_end=None, rows_stored=0, error=str(exc),
            started_at=started,
        )
        return {"rows_stored": 0, "error": str(exc), "window_start": None, "window_end": None}
    except Exception as exc:  # noqa: BLE001 - the screen must say something rather than hang
        logger.exception("projections refresh crashed")
        await repository.record_refresh(
            db, window_start=None, window_end=None, rows_stored=0,
            error=f"Unexpected error: {exc}", started_at=started,
        )
        return {"rows_stored": 0, "error": f"Unexpected error: {exc}",
                "window_start": None, "window_end": None}
