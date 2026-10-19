"""Mutation harness for Portfolio -> Analytics (17 Oct 2026).

Run: ``venv/Scripts/python scripts/mutate_analytics.py``

Every mutation MUST be caught. No "All N mutations caught" line means the harness crashed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
L, R, S, F, RF = ("app/analytics/logic.py", "app/analytics/repository.py", "app/analytics/service.py",
                  "app/analytics/fetch.py", "app/analytics/refresh.py")

MUTATIONS: list[tuple[str, str, str, str]] = [
    (L, "            \"mix\": round(delta - volume - price, 2)}", "            \"mix\": 0.0}",
     "the growth split no longer adds up"),
    (L, "        price += (now[\"sales\"] / now[\"units\"] - before[\"sales\"] / before[\"units\"]) * now[\"units\"]",
     "        price += (now[\"sales\"] / now[\"units\"] - before[\"sales\"] / before[\"units\"]) * before[\"units\"]",
     "price effect weighted by last month's units"),
    (L, "        volume = (k1 - k0) * (s0 / k0)", "        volume = (u1 - u0) * (s0 / u0)",
     "volume measured in packs instead of kilograms"),
    (L, "        volume, price = delta, 0.0", "        volume, price = 0.0, 0.0", "a launch is not counted as volume"),
    (L, "            \"conversion\": pct(units, sessions),", "            \"conversion\": pct(units, page_views),",
     "conversion over page views"),
    (L, "            bb_weight += r[\"buy_box_pct\"] * r[\"page_views\"]", "            bb_weight += r[\"buy_box_pct\"] * 1",
     "Buy Box averaged instead of weighted"),
    (L, "    return pct(sum(values[:n]), total) if total else None", "    return pct(sum(values[:n + 1]), total) if total else None",
     "top-10 share counts eleven"),
    (R, "            \"net_proceeds\": _float(row.net_proceeds) - sb_amount,", "            \"net_proceeds\": _float(row.net_proceeds),",
     "Sponsored Brands added to ads but left in Net"),
    (R, "        month = str(r.get(\"startDate\") or \"\")[:7]\n        if len(month) != 7:\n            continue",
     "        month = str(r.get(\"startDate\") or \"2025-01\")[:7]",
     "a dateless row filed under a guessed month"),
    (R, "        row[\"sessions\"] += int(traffic.get(\"sessions\") or 0)", "        row[\"sessions\"] = int(traffic.get(\"sessions\") or 0)",
     "a child split across two parents keeps one row's sessions (Aug 2026 halved)"),
    (R, "        await db.execute(delete(AnalyticsMonthly).where(AnalyticsMonthly.month.in_(months)))", "        pass",
     "a re-run doubles a month instead of replacing it"),
    (S, "        t[\"mom\"] = logic.growth(t[\"sales\"], totals[prev][\"sales\"]) if prev and m in complete else None",
     "        t[\"mom\"] = logic.growth(t[\"sales\"], totals[prev][\"sales\"]) if prev else None",
     "the half month is given a growth figure"),
    (S, "    complete = [m for m in months if m < current]", "    complete = [m for m in months if m <= current]",
     "the current month counts as complete"),
    (S, "        result = pf_logic.portfolio(months_rows[month], catalogue, {}, {}, include_inactive=include_inactive,",
     "        result = pf_logic.portfolio(months_rows[month], catalogue, {}, {}, include_inactive=True,",
     "inactive products shown by default"),
    (S, "    known_kg = all(e.get(\"kg\") is not None for e in entries if e.get(\"units\"))", "    known_kg = True",
     "an unknown weight is summed as if it were zero"),
    (F, "    last = min(date(year, mon, calendar.monthrange(year, mon)[1]), today - timedelta(days=1))",
     "    last = date(year, mon, calendar.monthrange(year, mon)[1])", "the current month asks for days not yet over"),
    (RF, "    finally:\n        STATE[\"running\"] = False", "    finally:\n        pass",
     "a finished or failed refresh leaves every later one refused"),
    ("app/scheduler.py", "ANALYTICS_REFRESH_IST = (9, 0)", "ANALYTICS_REFRESH_IST = (7, 0)", "analytics read before the sales pull"),
    (S, '    out["per_kg"] = logic.pct(sales, out["kg"])', '    out["per_kg"] = logic.pct(sales, out["units"])',
     "per kg divides by units"),
    (S, '            t.update(logic.weighted_rates(cat_traffic.get(name, {}).get(m, [])))', '            pass',
     "a category carries no traffic"),
    (S, '                      "per_kg": logic.pct(e["sales"], e.get("kg")),', '                      "per_kg": None,',
     "a product series has no per kg"),
]

TESTS = ["tests/test_analytics.py", "tests/test_retention_and_scheduler.py"]


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
