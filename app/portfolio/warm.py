"""Keep the Portfolio sub-tabs' answers built, so a click never waits for one.

Asked for as *"when I switch between different time periods, sub tabs or the sub sub tabs inside the
tab, everything should be seamless and as fast as possible"*. Measured on production before this:
the Profit data took 1.0 / 1.6 / 2.2 s for 7 / 30 / 60 days and Repeat customers 2.3-4.5 s, on every
click, while the arithmetic itself was a few hundredths of a second.

Every payload is cached per data version (`routers.portfolio._dashboard`, `repeat.service`,
`repeat.value_service`, `projections.service`), so the first click after the morning's refresh would
still pay the build. This runs every few minutes and builds what the screens open on. **When nothing
has changed it costs only the version checks** (a handful of MAX/COUNT queries and the shared sheet
read), because every call below is then a cache hit.

Read-only: it calls the same functions the pages call and writes nothing.
"""
from __future__ import annotations

import logging
import time

from app import ist
from app.database import async_session

logger = logging.getLogger(__name__)

#: The Profit tab's preset buttons.
PROFIT_PRESETS = (7, 30, 60, 90)


async def warm(db_factory=async_session) -> dict:
    """Build (or confirm cached) every payload a Portfolio sub-tab opens on. Never raises."""
    from app.portfolio import economics
    from app.projections import service as projections_service
    from app.repeat import service as repeat_service, value_service
    from app.routers import portfolio as pf_router

    started = time.monotonic()
    done: dict = {}
    today = ist.today()

    async def step(name, make):
        t = time.monotonic()
        try:
            async with db_factory() as db:
                await make(db)
            done[name] = round(time.monotonic() - t, 2)
        except Exception:  # noqa: BLE001 - a cache warm-up must never break anything
            logger.warning("portfolio warm-up: %s failed", name, exc_info=True)

    for days in PROFIT_PRESETS:
        window = economics.window_for(today, days)
        await step(f"profit {days}d", lambda db, w=window: pf_router._dashboard(db, w))
    await step("profit default", lambda db: pf_router._dashboard(db, None))
    await step("projections", lambda db: projections_service.build(db, include_inactive=False))
    await step("projections+inactive",
               lambda db: projections_service.build(db, include_inactive=True))
    await step("repeat", lambda db: repeat_service.build_payload(db, None, today))
    await step("customer value", lambda db: value_service.build_payload(db, None, today))
    total = round(time.monotonic() - started, 2)
    if total > 2:
        logger.info("portfolio warm-up: %.1fs %s", total, done)
    return done
