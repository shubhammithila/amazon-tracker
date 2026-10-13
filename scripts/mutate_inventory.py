"""Mutation harness for FBA stock from the Inventory API and "To purchase" (13 Oct 2026).

Run: ``venv/Scripts/python scripts/mutate_inventory.py``

Every mutation MUST be caught. No "All N mutations caught" line means the harness crashed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IL, IF, IR, IP = ("app/inventory/logic.py", "app/inventory/fetch.py", "app/inventory/refresh.py",
                  "app/inventory/repository.py")
PL, PS, SH, SC = ("app/projections/logic.py", "app/projections/service.py", "app/routers/shipment.py",
                  "app/scheduler.py")

MUTATIONS: list[tuple[str, str, str, str]] = [
    (IL, "HELD_FIELDS = QUANTITY_FIELDS[:-1]",
     'HELD_FIELDS = tuple(f for f in QUANTITY_FIELDS[:-1] if f != "inbound_shipped")',
     "shipped boxes are left out (the CSV's own bug)"),
    (IL, "HELD_FIELDS = QUANTITY_FIELDS[:-1]", "HELD_FIELDS = QUANTITY_FIELDS", "damaged stock counts as held"),
    (IL, "if not sku or not asin or _channel_of(sku) != CHANNEL_FBA:", "if not sku or not asin:",
     "flex rows and their 999 placeholder count as FBA stock"),
    (IL, "        out[r[\"asin\"]] = out.get(r[\"asin\"], 0) + int(r[\"held\"])",
     "        out[r[\"asin\"]] = int(r[\"held\"])", "a second FBA SKU on one ASIN overwrites the first"),
    (IL, "    return len(rows) >= ALL_ZERO_STOCK_THRESHOLD and not any(r[\"held\"] for r in rows)",
     "    return False", "dozens of SKUs all at zero is stored as a real empty warehouse"),
    (IF, "    raise InventoryFetchError(", "    return out\n    raise InventoryFetchError(",
     "a truncated answer is used as if complete"),
    (IP, "FbaInventoryRefresh.status == \"done\")", "FbaInventoryRefresh.status != \"running\")",
     "a failed run is read as current stock"),
    (PL, "    return round(max(0.0, total_stock - fba_kg), 1)", "    return round(total_stock - fba_kg, 1)",
     "a surplus becomes a negative purchase"),
    (PS, "            kg[key] = None\n            continue", "            continue",
     "an unknown pack weight makes FBA stock silently smaller"),
    (PS, "        r[\"fba_kg\"] = None if units is None else fba_kg.get(r[\"key\"], 0.0)",
     "        r[\"fba_kg\"] = fba_kg.get(r[\"key\"], 0.0)", "no stock reading yet reads as an empty FBA"),
    (PS, "        if key in shown_all or not units[child] or not weight:",
     "        if True:", "stock for a product with no row is dropped silently"),
    (SH, "    if units is not None and result.get(\"status\") == \"done\":", "    if units is not None:",
     "an old reading is passed off as a fresh API read"),
    (SH, "    stock, stock_info = await fba_stock_for_plan(stock_content)",
     "    stock, stock_info = parse_stock_csv(stock_content), {\"source\": \"api\", \"as_of\": None, \"warning\": None}",
     "the plan ignores the API stock"),
    (SH, "    has_total = \"afn-total-quantity\" in df.columns", "    has_total = False",
     "the file fallback goes back to the nine columns that missed shipped boxes"),
    (SC, "FBA_INVENTORY_IST = (7, 45)", "FBA_INVENTORY_IST = (7, 15)", "stock is read before the sales pull"),
]

TESTS = ["tests/test_inventory.py", "tests/test_projections.py", "tests/test_shipment_stock_csv.py",
         "tests/test_retention_and_scheduler.py"]


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
