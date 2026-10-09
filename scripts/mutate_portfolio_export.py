"""Mutation harness for the Portfolio download (filtered Excel + PDF).

Run: ``venv/Scripts/python scripts/mutate_portfolio_export.py``

Every mutation MUST be caught. Each breaks ONE decision; a survivor means a test asserts a conclusion
rather than the reason for it. No "All N mutations caught" line means the harness crashed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
E, T = "app/portfolio/export.py", "templates/portfolio.html"

MUTATIONS: list[tuple[str, str, str, str]] = [
    (E, "    if ids is None:\n        chosen = source", "    if True:\n        chosen = source",
     "the filter is ignored and every product is downloaded"),
    (E, "chosen = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]",
     "chosen = [r for r in source if key(r) in set(ids)]",
     "the screen's sort order is lost"),
    (E, '        collapsed = r.get("parent_asin") not in opened', "        collapsed = True",
     "a product expanded on screen arrives collapsed"),
    (E, '    "pct": "0.0%",', '    "pct": "General",',
     "percentages lose their % format"),
    (E, "    return row.get(col_id)\n", "    return row.get(col_id) or 0\n",
     "a missing figure becomes 0 instead of a blank"),
    (E, '        "tacos": _ratio(spend, sales),',
     '        "tacos": (sum((r.get("tacos") or 0) for r in rows) / len(rows)) if rows else None,',
     "the total TACOS is an average of rows, not recomputed from sums"),
    (E, '    "net_pct": "PRE-COGS:', '    "net_pct_off": "PRE-COGS:',
     "the pre-COGS caveat disappears from the file"),
    (E, '    ws.freeze_panes = freeze', '    ws.freeze_panes = None',
     "the headings and totals scroll away"),
    (E, "            return \"no sales\"\n        return row.get(\"acos\")",
     "            return None\n        return row.get(\"acos\")",
     "spend with no attributed sales reads as a blank"),
    (E, "    shown = [r for r in table.rows if not r.hidden]", "    shown = list(table.rows)",
     "the PDF prints collapsed sizes the screen does not show"),
    (E, "    wanted = [c for c in (columns if columns is not None else default_columns())\n"
        "              if c in COLUMN_KINDS]",
     "    wanted = [c for c in (columns if columns is not None else default_columns())]",
     "an unknown column id reaches the table"),
    (E, '    skus = view == "skus"', '    skus = False',
     "the SKU view downloads products"),
    (E, '        cell.font, cell.fill = bold, head_fill', '        cell.font, cell.fill = base, head_fill',
     "cosmetic header change — must be caught by the Arial/heading checks or is not tested"),
    (T, "    ids: visible().map(r => skus ? r.asin : r.parent_asin),",
     "    ids: rows().map(r => skus ? r.asin : r.parent_asin),",
     "the page sends every row instead of the filtered ones"),
    (T, "    columns: visibleColumns().slice(1).map(c => c.id),",
     "    columns: (data.columns || []).map(c => c.id),",
     "the page sends every column instead of the visible ones"),
]

TESTS = [
    "tests/test_portfolio_export.py", "tests/test_portfolio_weight.py", "tests/test_portfolio_api.py",
    "tests/test_portfolio_screen.py",
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
