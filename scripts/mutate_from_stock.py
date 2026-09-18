"""Mutation harness for the made-today / from-stock split.

Run: ``venv/Scripts/python scripts/mutate_from_stock.py``

Every mutation MUST be caught. The stakes are why: ``units`` is the number that reaches a GST
invoice and the Amazon upload quantity, so a mutation that makes it mean "made today" instead of
the total under-bills a tax document and under-declares a shipment — and the boxes still ship, so
it surfaces at reconciliation rather than at save time.

This codebase has a documented record of green suites hiding exactly that shape of bug: the FBA
packing information (2,203 tests and 19/19 mutations passing while the flow was broken), the SB
``daily=True`` fetch, the deploy detector's revision id, the pause feature shipping broken three
times.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: (file, find, replace, why this would be a real bug)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── The arithmetic ──
    (
        "app/shipment/logic.py",
        "    return max(0, _entry_field(entry, \"units\") - _entry_field(entry, \"from_stock\"))",
        "    return _entry_field(entry, \"units\")",
        "made-today ignores the split, so the accounts sheet reports the total twice",
    ),
    (
        "app/shipment/logic.py",
        "    return max(0, _entry_field(entry, \"units\") - _entry_field(entry, \"from_stock\"))",
        "    return _entry_field(entry, \"units\") - _entry_field(entry, \"from_stock\")",
        "made-today can go NEGATIVE, which reads as a broken report",
    ),
    (
        "app/shipment/logic.py",
        "            bucket[\"units\"] += _entry_field(entry, \"units\")",
        "            bucket[\"units\"] += _entry_field(entry, \"made_today\")",
        "the packed sheet's total becomes made-today, disagreeing with the invoice",
    ),
    # ── The store ──
    (
        "app/shipment/repository.py",
        "        from_stock = min(units, max(0, _as_int(raw.get(\"from_stock\"))))",
        "        from_stock = max(0, _as_int(raw.get(\"from_stock\")))",
        "from_stock above the total is stored, making made-today negative",
    ),
    (
        "app/shipment/repository.py",
        "                    from_stock=from_stock,\n",
        "",
        "from_stock is never stored on a new row — the split is silently lost",
    ),
    (
        "app/shipment/repository.py",
        "            row.from_stock = from_stock",
        "            pass",
        "from_stock is never UPDATED, so a correction does not take",
    ),
    (
        "app/shipment/repository.py",
        '                "total_from_stock": logic.from_stock_units(entries),',
        '                "total_from_stock": 0,',
        "the owner's day card always reads zero from stock",
    ),
    # ── The screen: the total must be the SUM ──
    (
        "templates/ops.html",
        "  return Number(i.made_today || 0) + Number(i.from_stock || 0);",
        "  return Number(i.made_today || 0);",
        "the screen sends made-today as the total — the GST/Amazon under-report",
    ),
    (
        "templates/ops.html",
        "      units: rowUnits(i),",
        "      units: Number(i.made_today || 0),",
        "the payload's units drops the shelf quantity",
    ),
    (
        "templates/ops.html",
        "      from_stock: Number(i.from_stock || 0),\n",
        "",
        "the screen never sends from_stock, so nothing is ever recorded",
    ),
    # ── The sheet for accounts ──
    (
        "app/routers/shipment.py",
        'documents.IDENTITY_HEADERS + ["Units", "Made today", "From stock"],',
        'documents.IDENTITY_HEADERS + ["Made today", "From stock", "Units"],',
        "the total no longer leads the sheet accounts reconciles against",
    ),
    (
        "app/routers/shipment.py",
        '                int(counts.get("made_today", 0)),\n                int(counts.get("from_stock", 0)),',
        '                int(counts.get("from_stock", 0)),\n                int(counts.get("made_today", 0)),',
        "made-today and from-stock are swapped on the accounts sheet",
    ),
    # ── The packing screen's own payload ──
    (
        "app/routers/shipment.py",
        '                "made_today": logic.made_today(mine),',
        '                "made_today": int(mine.get("units") or 0),',
        "the screen prefills made-today with the total, doubling it on a re-save",
    ),
]


def run_tests() -> bool:
    proc = subprocess.run(
        [
            str(ROOT / "venv/Scripts/python"),
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:randomly",
            "tests/test_packing_from_stock.py",
            "tests/test_ops_ui.py",
            "tests/test_shipment_documents.py",
            "tests/test_shipment_plan_db.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    survivors: list[str] = []
    for index, (rel, find, replace, why) in enumerate(MUTATIONS, 1):
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        label = f"[{index:2}/{len(MUTATIONS)}]"

        if find not in original:
            print(f"{label} SKIP  target text not found in {rel}")
            print(f"          looked for: {find[:78]!r}")
            survivors.append(f"{rel}: target not found — {why}")
            continue

        backup = tempfile.mktemp(suffix=".bak")
        shutil.copy2(path, backup)
        try:
            path.write_text(original.replace(find, replace, 1), encoding="utf-8")
            if run_tests():
                print(f"{label} SURVIVED  {why}")
                survivors.append(f"{rel}: {why}")
            else:
                print(f"{label} caught    {why}")
        finally:
            shutil.copy2(backup, path)
            Path(backup).unlink(missing_ok=True)

    print()
    if survivors:
        print(f"{len(survivors)} MUTATION(S) SURVIVED — a test is missing:")
        for item in survivors:
            print("  -", item)
        return 1
    print(f"All {len(MUTATIONS)} mutations caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
