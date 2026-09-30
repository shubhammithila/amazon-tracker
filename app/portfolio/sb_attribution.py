"""Copy Sponsored Brands spend onto the Portfolio's per-day rows, attributed per ASIN.

The Portfolio tab's ad spend was Sponsored Products ONLY: Amazon's economics feed returns one ad type
name, `SponsoredProductFee`, across all 9,074 stored rows that carry ad spend. That hid **28% of real
ad spend** from every row and every KPI tile. See `EconomicsDaily.sb_spend`.

── Where the SB spend comes from, and why it is NOT a second Amazon report ──

The first design had the Portfolio refresh create its own `sbTargeting` report. That would have
reintroduced the Ads tab's worst week: **SB report creation is throttled over a window of HOURS,
counted across every report created that day** (measured: 429 after 15 minutes of complete idleness),
and the Ads tab's nightly job creates one at 08:00 IST. A Portfolio report at 07:30 would spend that
budget first and bring back "Sponsored Brands figures are stale".

So this reads the SB spend the Ads tab ALREADY fetches every night — `ads_performance_daily`, per
`(day, ad_group_id)` — and COPIES the attributed figure onto `economics_daily`. Zero extra reports.

**Copied at write time, never read at request time**, and that distinction is load-bearing:
`ads_performance_daily` keeps 60 days and `economics_daily` keeps 90. Reading SB at request time would
make a 90-day window silently SP-only for its first third. Copied nightly while each day is inside the
Ads tab's 60, the figure then lives on the Portfolio row for its full 90.

── When it runs ──

  * **After each SB chunk the Ads refresh stores** (`app/ads/refresh.py`), for exactly those days.
    That is the nightly path: Portfolio stores yesterday's economics at 07:30, Ads stores yesterday's
    SB at 08:00, and attribution runs then, with both halves present.
  * **After a manual Portfolio refresh**, for its window.
  * **From `scripts/backfill_portfolio_sb.py`**, once, for the history.

**A day is attributed only when BOTH halves are held.** A day with no SB rows may simply not have
been fetched (a throttled report) and a day with no economics has no sales to weight by — attributing
either would zero a real figure or lose the spend. Such a day is skipped and picked up by whichever
job completes it second.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AdsPerformanceDaily, EconomicsDaily
from app.portfolio import ads, logic, repository

logger = logging.getLogger(__name__)

#: `ads_performance_daily.ad_product` for Sponsored Brands.
SB_PRODUCT = "sb"


async def load_sb_ad_group_spend(
    db: AsyncSession, days: Iterable[str]
) -> dict[tuple[str, str], float]:
    """``{(day, ad_group_id): spend}`` from the Ads tab's own SB rows, summed over keywords/targets."""
    day_list = sorted(set(days))
    if not day_list:
        return {}
    rows = await db.execute(
        select(AdsPerformanceDaily.day, AdsPerformanceDaily.ad_group_id,
               func.sum(AdsPerformanceDaily.spend))
        .where(AdsPerformanceDaily.ad_product == SB_PRODUCT,
               AdsPerformanceDaily.day.in_(day_list))
        .group_by(AdsPerformanceDaily.day, AdsPerformanceDaily.ad_group_id)
    )
    return {(day, str(group or "")): float(spend or 0) for day, group, spend in rows}


async def _days_with_economics(db: AsyncSession, days: Iterable[str]) -> set[str]:
    day_list = sorted(set(days))
    if not day_list:
        return set()
    rows = await db.execute(
        select(EconomicsDaily.day).distinct()
        .where(EconomicsDaily.day.in_(day_list),
               EconomicsDaily.seller_sku == repository.ASIN_GRAIN)
    )
    return {day for (day,) in rows}


async def fetch_ad_group_asins() -> dict[str, list[str]]:
    """Amazon's declaration of which ASINs each SB ad group advertises. One list call, not a report."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
        return await ads.fetch_sb_ad_asins(client)


def combine(
    ad_group_spend: Mapping[tuple[str, str], float],
    ad_group_asins: Mapping[str, list[str]],
    sales: Mapping[tuple[str, str], float],
) -> tuple[dict[tuple[str, str], tuple[float, str]], dict]:
    """Declared + spread, per DAY, as ``{(day, asin): (amount, basis)}`` plus a reconciliation.

    **Spread per day, over that day's sales**, not over the window: the store is per day so every
    sub-range is a sum, and a window-level spread would make a 7-day slice of it wrong.

    The reconciliation is returned rather than only logged, so the backfill can print it and a test
    can assert it: `declared + spread + lost == total`. `lost` is non-zero only on a day where
    nothing at all sold, which has no sales to weight a spread by.
    """
    by_day: dict[str, dict[tuple[str, str], float]] = {}
    for key, spend in ad_group_spend.items():
        by_day.setdefault(key[0], {})[key] = spend

    out: dict[tuple[str, str], tuple[float, str]] = {}
    declared_total = spread_total = lost = 0.0
    for day, spend in sorted(by_day.items()):
        day_sales = {k: v for k, v in sales.items() if k[0] == day}
        declared, unattributable = logic.allocate_sb_spend(spend, ad_group_asins, day_sales)
        spread = logic.spread_unattributable(unattributable, day_sales)
        declared_total += sum(declared.values())
        spread_total += sum(spread.values())
        lost += unattributable - sum(spread.values())
        for key in set(declared) | set(spread):
            amount = declared.get(key, 0.0) + spread.get(key, 0.0)
            basis = logic.SB_BASIS_DECLARED if declared.get(key) else logic.SB_BASIS_SPREAD
            out[key] = (round(amount, 2), basis)

    return out, {
        "total": round(sum(ad_group_spend.values()), 2),
        "declared": round(declared_total, 2),
        "spread": round(spread_total, 2),
        "lost": round(lost, 2),
    }


async def attribute_days(
    db: AsyncSession,
    days: Iterable[str],
    *,
    ad_group_asins: Mapping[str, list[str]] | None = None,
) -> dict:
    """Attribute SB spend onto every day in ``days`` that holds BOTH halves. Returns a summary.

    ``ad_group_asins`` is fetched from Amazon when not given — one list call. **If that fetch fails
    this raises and writes nothing**, so the last good figures stand: attributing with an empty map
    would send 100% of SB spend to the spread bucket, a plausible-looking wrong answer.
    """
    wanted = set(days)
    spend = await load_sb_ad_group_spend(db, wanted)
    with_sb = {day for day, _ in spend}
    ready = with_sb & await _days_with_economics(db, with_sb)
    if not ready:
        return {"days": 0, "total": 0.0, "declared": 0.0, "spread": 0.0, "lost": 0.0}

    if ad_group_asins is None:
        ad_group_asins = await fetch_ad_group_asins()

    spend = {k: v for k, v in spend.items() if k[0] in ready}
    sales = await repository.load_sales_by_day_asin(db, ready)
    allocations, summary = combine(spend, ad_group_asins, sales)
    await repository.save_sb_spend(db, ready, allocations)

    summary["days"] = len(ready)
    if summary["lost"]:
        logger.warning(
            "portfolio SB: Rs %.2f could not be attributed (a day with no sales at all)",
            summary["lost"],
        )
    logger.info(
        "portfolio SB: %d day(s), Rs %.2f total = %.2f declared + %.2f spread",
        summary["days"], summary["total"], summary["declared"], summary["spread"],
    )
    return summary
