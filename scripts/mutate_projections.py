"""Mutation harness for Portfolio -> Projections (the live stock-to-hold table).

Run: ``venv/Scripts/python scripts/mutate_projections.py``

Every mutation MUST be caught. No "All N mutations caught" line means the harness crashed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
L, S, R = "app/projections/logic.py", "app/projections/service.py", "app/routers/projections.py"
P, PF = "templates/projections.html", "app/routers/portfolio.py"

MUTATIONS: list[tuple[str, str, str, str]] = [
    (L, "    return weight * (kg_7d / 7) + (1 - weight) * rate_30",
     "    return (1 - weight) * (kg_7d / 7) + weight * rate_30", "the 30-day rate gets the bigger weight"),
    (L, "    if kg_7d is None:\n        return rate_30", "    if kg_7d is None:\n        return 0.0",
     "a missing 7-day window reads as a zero week"),
    (L, "    buffer_days = sum(legs[leg] * settings[slip] / 100 for leg, slip in LEGS)",
     "    buffer_days = max(legs[leg] * settings[slip] / 100 for leg, slip in LEGS)", "the buffer takes only the worst slip"),
    (L, "    ideal_fba = projected_daily * lead_total * markup",
     "    ideal_fba = projected_daily * (lead_total - legs['supplier_to_wh']) * markup",
     "Ideal FBA leaves out supplier to warehouse"),
    (L, "    ideal_wh = projected_daily * buffer_days * markup", "    ideal_wh = projected_daily * buffer_days",
     "the markup skips the warehouse half"),
    (L, "    projected_daily = base * seasonal * (1 + settings[\"global_growth_rate\"])",
     "    projected_daily = base * seasonal", "growth is ignored"),
    (L, "    \"stock_markup\": (1.0, 5.0),", "    \"stock_markup\": (0.0, 5.0),", "a markup below 1 is accepted"),
    (L, "    if field != \"seasonal_impact\" and number != int(number):", "    if False:",
     "half a day of lead time is accepted"),
    (L, "        return round(sum(vals), 1) if vals else None", "        return round(sum(vals), 1)",
     "a set with no figures totals 0 kg"),
    (S, "        row[\"kg_7d\"] = None if seven is None else (seven.get(row[\"key\"]) or 0.0)",
     "        row[\"kg_7d\"] = None if seven is None else seven.get(row[\"key\"])",
     "a product that sold nothing this week is treated as unknown"),
    (S, "    out.sort(key=lambda r: (rank.get(r[\"category\"], len(rank)), -(r[\"kg_30d\"] or 0), r[\"product\"].casefold()))",
     "    out.sort(key=lambda r: r[\"product\"].casefold())", "the packing order is lost"),
    (S, "    named = stored_by_name.get(logic.normalize_name(row[\"product\"]))\n    if named is not None:\n        return named, \"saved\", named[\"id\"]\n",
     "", "earlier lead times are not carried over by name"),
    (S, "    d7 = await pf_router._dashboard(db, w[\"7\"], include_inactive=include_inactive)",
     "    d7 = await pf_router._dashboard(db, w[\"7\"])", "the inactive toggle is ignored for the 7-day window"),
    (R, "        full = {field: row[field] for field in logic.EDITABLE}\n        full.update(values)",
     "        full = values", "saving one field resets the others to defaults"),
    (R, "    cleaned = []\n    for change in changes:",
     "    cleaned = []\n    for change in changes[:1]:", "only the first change is validated"),
    (PF, "        stock = {}\n", "        raise\n", "a projections failure takes the Profit tab down"),
    (P, '$("save-btn").disabled = !count || bad;', '$("save-btn").disabled = bad;',
     "save is active with nothing changed"),
]

TESTS = ["tests/test_projections.py"]


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
