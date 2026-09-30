"""One-off: attribute Sponsored Brands spend onto the Portfolio's stored history.

Run on production after migration `b91d4a7c3e26`, in a `screen`:

    cd /opt/amazon-tracker && venv/bin/python scripts/backfill_portfolio_sb.py
    cd /opt/amazon-tracker && venv/bin/python scripts/backfill_portfolio_sb.py --fetch-missing

Before this, every Portfolio figure was Sponsored Products only — Amazon's economics feed returns a
single ad type, `SponsoredProductFee`. See `app/portfolio/sb_attribution.py`.

── What it does ──

1. For every day `economics_daily` holds, attributes the SB spend the Ads tab ALREADY stored in
   `ads_performance_daily`. No Amazon report, one list call for the ad-group -> ASIN map.
2. **Names the days it could not cover** rather than leaving them silently zero. `ads_performance_daily`
   keeps 60 days and `economics_daily` 90, so the oldest ~29 are expected here — and a zero there is
   indistinguishable from "no SB ran that day".
3. With `--fetch-missing`, asks Amazon for an SB report covering ONLY those days and attributes from
   it directly, without storing it in the Ads tab's table (which would purge it the next night).
   **Opt-in**, because SB report creation is throttled over HOURS across every report created that
   day, and the Ads tab's nightly job needs one at 08:00 IST. Do not run it near then.

**Idempotent.** `save_sb_spend` resets every row of the days it writes, so a second run corrects
rather than doubles.

It ends by RECONCILING rather than reporting success because a fetch returned 200: the SB rupees on
the Portfolio rows must equal the Ads tab's own SB total for the same days.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.database import async_session  # noqa: E402
from app.models import AdsPerformanceDaily, EconomicsDaily  # noqa: E402
from app.portfolio import repository, sb_attribution  # noqa: E402


async def _days(db, model, *filters) -> set[str]:
    rows = await db.execute(select(model.day).distinct().where(*filters))
    return {day for (day,) in rows}


async def _portfolio_sb(db, days) -> float:
    if not days:
        return 0.0
    total = await db.execute(
        select(func.sum(EconomicsDaily.sb_spend)).where(
            EconomicsDaily.day.in_(sorted(days)),
            EconomicsDaily.seller_sku == repository.ASIN_GRAIN,
        )
    )
    return float(total.scalar() or 0)


async def _fetch_missing(db, missing: list[str], ad_group_asins) -> dict:
    """SB spend for days the Ads tab no longer holds, straight from Amazon, attributed, not stored."""
    from app.ads import reports

    raw = await reports.fetch_targeting(missing[0], missing[-1], ad_product="sb", daily=True)
    wanted = set(missing)
    spend: dict[tuple[str, str], float] = {}
    for row in raw:
        day = str(row.get("date") or "")[:10]
        if day not in wanted:                  # a dateless row is SKIPPED, never guessed a day
            continue
        key = (day, str(row.get("adGroupId") or ""))
        spend[key] = spend.get(key, 0.0) + float(row.get("cost") or 0)

    ready = {day for day, _ in spend}
    sales = await repository.load_sales_by_day_asin(db, ready)
    allocations, summary = sb_attribution.combine(spend, ad_group_asins, sales)
    await repository.save_sb_spend(db, ready, allocations)
    summary["days"] = len(ready)
    return summary


async def main(fetch_missing: bool) -> int:
    async with async_session() as db:
        econ_days = await _days(db, EconomicsDaily, EconomicsDaily.seller_sku == repository.ASIN_GRAIN)
        sb_days = await _days(db, AdsPerformanceDaily, AdsPerformanceDaily.ad_product == "sb")
        if not econ_days:
            print("no economics days stored — run scripts/backfill_portfolio_daily.py first")
            return 1

        print(f"economics days held : {len(econ_days)}  ({min(econ_days)}..{max(econ_days)})")
        print(f"Ads-tab SB days held: {len(sb_days)}"
              + (f"  ({min(sb_days)}..{max(sb_days)})" if sb_days else ""))

        ad_group_asins = await sb_attribution.fetch_ad_group_asins()
        print(f"SB ad groups        : {len(ad_group_asins)} "
              f"({sum(1 for v in ad_group_asins.values() if v)} naming an ASIN)")

        summary = await sb_attribution.attribute_days(db, econ_days, ad_group_asins=ad_group_asins)
        print()
        print(f"attributed          : {summary['days']} day(s)")
        print(f"  SB spend          : Rs {summary['total']:,.2f}")
        print(f"  declared          : Rs {summary['declared']:,.2f}")
        print(f"  spread            : Rs {summary['spread']:,.2f}")
        print(f"  lost (no sales)   : Rs {summary['lost']:,.2f}")

        covered = econ_days & sb_days
        missing = sorted(econ_days - sb_days)

        # ── Reconciliation: the only claim that proves anything ──
        ads_total = await db.execute(
            select(func.sum(AdsPerformanceDaily.spend)).where(
                AdsPerformanceDaily.ad_product == "sb",
                AdsPerformanceDaily.day.in_(sorted(covered)),
            )
        )
        ads_sb = float(ads_total.scalar() or 0)
        portfolio_sb = await _portfolio_sb(db, covered)
        gap = portfolio_sb - ads_sb
        print()
        print(f"RECONCILE over {len(covered)} day(s) both halves hold:")
        print(f"  Ads tab SB total   Rs {ads_sb:,.2f}")
        print(f"  Portfolio SB total Rs {portfolio_sb:,.2f}")
        print(f"  difference         Rs {gap:,.2f}  "
              + ("OK" if abs(gap) < 1.0 else "<-- DOES NOT RECONCILE"))

        if missing:
            print()
            print(f"{len(missing)} day(s) have economics but NO Ads-tab SB rows, and read SP-only:")
            print(f"  {missing[0]}..{missing[-1]}"
                  + ("" if len(missing) < 6 else f"  (first: {', '.join(missing[:5])})"))
            if fetch_missing:
                print("  --fetch-missing: asking Amazon for an SB report over those days...")
                extra = await _fetch_missing(db, missing, ad_group_asins)
                print(f"  attributed {extra['days']} more day(s), Rs {extra['total']:,.2f} "
                      f"(declared {extra['declared']:,.2f}, spread {extra['spread']:,.2f})")
            else:
                print("  They age out of the 90-day window by themselves. To fill them now, rerun")
                print("  with --fetch-missing — NOT near 08:00 IST (the Ads tab's SB report).")

        return 0 if abs(gap) < 1.0 else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fetch-missing", action="store_true",
                        help="create an SB report for days the Ads tab no longer holds")
    sys.exit(asyncio.run(main(parser.parse_args().fetch_missing)))
