"""Mutation harness for Sponsored Brands in the Portfolio figures.

Run: ``venv/Scripts/python scripts/mutate_portfolio_sb.py``

Every mutation MUST be caught. Each breaks ONE decision; a survivor means a test asserts a conclusion
rather than the reason for it. Mutations DELETE or REPLACE code rather than dead-coding a guard with
`if(False and ...)`, which leaves the rendered source intact — see CLAUDE.md.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "app/portfolio/repository.py"
LOGIC = "app/portfolio/logic.py"
ATTR = "app/portfolio/sb_attribution.py"
ADS_REFRESH = "app/ads/refresh.py"

MUTATIONS: list[tuple[str, str, str, str]] = [
    (REPO, '        bucket["net_proceeds"] -= sb\n', "",
     "**margin stays overstated while TACOS reads correct** — netProceeds already nets SP, so SB "
     "must come out of it too, or the two figures contradict each other on one row"),
    (REPO, 'for name, amount in sorted(_with_sb(agg).items())',
     'for name, amount in sorted(agg["ad_types"].items())',
     "SB never reaches size_row, which reads ad spend from the ads LIST — the fix that changes no "
     "figure on screen while reading exactly like the fix"),
    (REPO, '        bucket["sb_spend"] += sb\n', "",
     "SB is taken out of net but never added to ad spend"),
    (REPO, '''            row["sb_spend"], row["sb_basis"] = carried.get(
                (row["day"], row["child_asin"]), (0.0, "")
            )''', '''            row["sb_spend"], row["sb_basis"] = (0.0, "")''',
     "a manual re-fetch wipes the attributed SB, and a throttled SB report leaves the day SP-only"),
    (REPO, '''        .where(EconomicsDaily.day.in_(day_list), EconomicsDaily.seller_sku == ASIN_GRAIN)
        .values(sb_spend=0, sb_basis="")''', '''        .where(EconomicsDaily.seller_sku == ASIN_GRAIN)
        .values(sb_spend=0, sb_basis="")''',
     "the nightly one-day allocation resets the SB figure on the other 89 days"),
    (REPO, '''        .where(EconomicsDaily.day.in_(day_list), EconomicsDaily.seller_sku == ASIN_GRAIN)
        .values(sb_spend=0, sb_basis="")
    )''', '''        .where(EconomicsDaily.day.in_(day_list), EconomicsDaily.seller_sku == "never")
        .values(sb_spend=0, sb_basis="")
    )''',
     "no reset: an ASIN that stopped receiving SB keeps yesterday's figure for ever"),
    (LOGIC, "weights = {asin: _num(sales_by_day_asin.get((day, asin))) for asin in asins}",
     "weights = {asin: 1.0 for asin in asins}",
     "EQUAL split — the rejected basis: a size that sold nothing is billed Rs 6,114"),
    (LOGIC, '''            if (day, asin) in sales_by_day_asin:
                allocated[(day, asin)] = allocated.get((day, asin), 0.0) + amount
            else:
                unattributable += amount''',
     '''            allocated[(day, asin)] = allocated.get((day, asin), 0.0) + amount''',
     "spend is attributed to an ASIN with no economics row, where it can never be stored"),
    (LOGIC, '''        if total_weight <= 0:
            unattributable += amount           # property 3
            continue''', "",
     "a whole group that sold nothing that day divides by zero"),
    (LOGIC, '''            if index == len(share_asins) - 1:
                share = amount - running
            else:
                share = round(amount * weights[asin] / total_weight, 2)
                running += share''',
     '''            share = round(amount * weights[asin] / total_weight, 2)''',
     "per-share rounding drifts a paisa per ASIN, so the allocation stops conserving"),
    (ATTR, "        day_sales = {k: v for k, v in sales.items() if k[0] == day}",
     "        day_sales = dict(sales)",
     "the spread uses the WINDOW's sales, so a sub-range of a multi-day allocation is wrong"),
    (ATTR, "    ready = with_sb & await _days_with_economics(db, with_sb)",
     "    ready = with_sb",
     "a day with SB but no economics yet is attributed with no sales — the spend is lost"),
    (ATTR, "    with_sb = {day for day, _ in spend}", "    with_sb = set(wanted)",
     "a day the Ads tab has NOT fetched (a throttled report) is zeroed on no evidence"),
    (ATTR, '''    if ad_group_asins is None:
        ad_group_asins = await fetch_ad_group_asins()''', '''    if ad_group_asins is None:
        try:
            ad_group_asins = await fetch_ad_group_asins()
        except Exception:
            ad_group_asins = {}''',
     "a failed ASIN list sends 100% of SB to the spread bucket — plausible and wrong"),
    (ADS_REFRESH, '''    except Exception:  # noqa: BLE001 - must never fail the Ads tab's own refresh
        logger.warning("ads refresh: attributing SB spend to the Portfolio failed", exc_info=True)''',
     '''    except Exception:  # noqa: BLE001 - must never fail the Ads tab's own refresh
        raise''',
     "a Portfolio attribution bug now fails the Ads tab's own SB report"),
    (ADS_REFRESH, "            await _attribute_sb_to_portfolio(db_factory, chunk_start, chunk_end)\n",
     "", "the nightly path is gone: Portfolio SB never updates after the first backfill"),
    ("app/portfolio/refresh.py", "        # ── Sponsored Brands, copied from the Ads tab's own stored rows ──\n",
     "        await __import__('app.ads.reports', fromlist=['x']).fetch_targeting\n"
     "        # ── Sponsored Brands, copied from the Ads tab's own stored rows ──\n",
     "the Portfolio refresh starts creating its own SB report, spending the Ads tab's throttle budget"),
    ("deploy/update-ec2.sh", '''elif "sb_spend" in cols("economics_daily"):
    print("b91d4a7c3e26")                           # head: SB spend attributed per ASIN per day
''', "", "the baseline detector is stale and stamps production BACKWARDS — a failed deploy"),
]

TESTS = [
    "tests/test_portfolio_sb.py",
    "tests/test_schema_migrations.py",
    "tests/test_portfolio_daily.py",
]


def main() -> int:
    python = str(ROOT / "venv" / "Scripts" / "python.exe")
    if not Path(python).exists():
        python = str(ROOT / "venv" / "bin" / "python")
    survivors = []
    for index, (rel, find, replace, why) in enumerate(MUTATIONS, 1):
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        label = f"[{index:2}/{len(MUTATIONS)}]"
        if find not in original:
            print(f"{label} SKIP      target not found in {rel}: {find[:60]!r}")
            survivors.append(f"{rel}: target not found — {why}")
            continue
        backup = tempfile.mktemp(suffix=".bak")
        shutil.copy2(path, backup)
        try:
            path.write_text(original.replace(find, replace, 1), encoding="utf-8")
            result = subprocess.run(
                [python, "-m", "pytest", "-q", "-x", "-p", "no:randomly", *TESTS],
                cwd=ROOT, capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"{label} SURVIVED  {why}")
                survivors.append(f"{rel}: {why}")
            else:
                print(f"{label} caught    {why}")
        finally:
            shutil.copy2(backup, path)
            Path(backup).unlink(missing_ok=True)
    print()
    if survivors:
        print(f"{len(survivors)} MUTATION(S) SURVIVED:")
        for item in survivors:
            print("  -", item)
        return 1
    print(f"All {len(MUTATIONS)} mutations caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
