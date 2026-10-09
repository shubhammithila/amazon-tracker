"""Mutation harness for Portfolio -> Repeat customers.

Run: ``venv/Scripts/python scripts/mutate_repeat.py``

Every mutation MUST be caught. Each breaks ONE decision; a survivor means a test asserts a conclusion
rather than the reason for it — strengthen the TEST. Mutations DELETE or REPLACE code, never
dead-code a guard. No "All N mutations caught" line means the harness crashed, which is not a pass.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
L, P, K = "app/repeat/logic.py", "app/repeat/parse.py", "app/repeat/keys.py"
R, F, S = "app/repeat/repository.py", "app/repeat/refresh.py", "app/repeat/service.py"
T = "templates/portfolio_repeat.html"
X = "app/repeat/export.py"

MUTATIONS: list[tuple[str, str, str, str]] = [
    (L, "after = [o for o in history if anchor < o.day <= anchor + span]",
     "after = [o for o in history if anchor <= o.day <= anchor + span and o.day != anchor - span]",
     "a same-day order counts as a repeat"),
    (L, "before = [o for o in history if anchor - span <= o.day < anchor]",
     "before = [o for o in history if anchor - span <= o.day <= anchor]",
     "a product in the SAME order counts as a cross flow"),
    (L, "                    anchors.setdefault(p, o.day)", "                    anchors[p] = o.day",
     "the anchor moves to the LAST purchase in the period"),
    (L, "    if whole < MIN_COHORT:", "    if whole < 1:",
     "percentages shown for a handful of buyers"),
    (L, '                bc["buyers"] += 1', '                bc["buyers"] += len(anchors)',
     "the brand total counts a customer once per product"),
    (L, "    if history_from is None or history_from > need:",
     "    if history_from is None or history_from > need + timedelta(days=60):",
     "a window without enough look-back history is reported"),
    (L, "    return end - timedelta(days=PERIOD_DAYS - 1), end",
     "    return as_of - timedelta(days=PERIOD_DAYS - 1), as_of",
     "the cohort period gets no follow-up time at all"),
    (L, "        if a <= until <= b:\n            return a", "        return a",
     "coverage ignores a gap in the stored history"),
    (P, 'return (price or "").strip() != "" and float(price) == 0.0', "return False",
     "free replacements become repeat purchases"),
    (P, "    return ist.day_of(when)", "    return when.date().isoformat()",
     "purchase day in UTC, a day off for 5.5 hours daily"),
    (K, "normal = (email or \"\").strip().lower()", "normal = (email or \"\").strip()",
     "one buyer splits into two keys by letter case"),
    (R, "            await db.execute(delete(CustomerOrderLine).where(CustomerOrderLine.id.in_(ids)))",
     "            pass", "a re-read of an overlapping window doubles the rows"),
    (F, "                                                    error=error)\n                    break",
     "                                                    error=error)\n                    continue",
     "a failed chunk is skipped and later chunks claim the coverage"),
    (S, "today - timedelta(days=1))", "today)",
     "as_of reaches today's incomplete day"),
    (S, "        if not everything and brand_of.get(p) != brand:\n            continue\n", "",
     "other brands leak into the Mithila table"),
    (T, "cross ? c.came_from_pct : null", "c.came_from_pct",
     "the brand total shows a cross flow it cannot have"),
    (T, "const partial = r.fba_share != null && r.fba_share < data.fba_partial_below;",
     "const partial = false;", "Easy-Ship-heavy rows are not flagged"),
    (T, "const c = win.available ? (w[n] || {}) : {};", "const c = w[n] || {};",
     "an unavailable window's figures leak onto the screen"),
    (L, """                bought = sum(o.units_of(p) for o in history
                             if anchor <= o.day <= anchor + span)""",
     """                bought = sum(o.units_of(p) for o in history
                             if anchor < o.day <= anchor + span)""",
     "repeat units leave out the first purchase (not Brand Analytics' measure)"),
    (L, "                    c[\"repeat_units\"] += bought\n", "",
     "repeat units are never counted"),
    (L, "    if buyers < MIN_COHORT or units <= 0:", "    if units <= 0:",
     "a repeat-units share is shown for a 3-customer cohort"),
    (L, "        o[3][line[\"parent_asin\"]] += int(line.get(\"units\") or 0)",
     "        o[3][line[\"parent_asin\"]] = 1",
     "units are counted as one per line"),
    (T, "    + cellPct(`units-${n}`, c.units_pct, why)", "    + cellPct(`units-${n}`, c.same_pct, why)",
     "the Repeat units column shows the customer share"),
    (T, "  return category ? data.rows.filter(r => r.category === category) : data.rows;",
     "  return data.rows;", "picking a category does not filter the table"),
    (T, "(g ? g.total : data.total)", "data.total", "a category shows the brand's total"),
    (T, 'const WINS = ["90", "60", "30"];', 'const WINS = ["30", "60", "90"];',
     "the windows are back to 30 first"),
    (T, "{format, brand: data.brand, category, ids: sortedRows().map(r => r.parent_asin)}",
     "{format, brand: data.brand, category}", "the download ignores the screen's rows and order"),
    (X, "        chosen = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]",
     "        chosen = list(by_id.values())", "the download ignores the ids it was sent"),
    (X, '            f"same-{n}": c.get("same_pct", c.get("repeat_pct")),',
     '            f"same-{n}": c.get("units_pct"),', "the download puts units in the customer column"),
    (S, "                      group_of=category_of)", "                      group_of=brand_of)",
     "category totals are computed over brands"),
    (S, "    if allc.get(p, 0) <= 0:", "    if not allc.get(p):", "net-negative units still get a share"),
    (S, "        line[\"parent_asin\"] = flavour_of.get(line[\"child_asin\"]) or line[\"parent_asin\"]",
     "        pass", "flavours are not split on the Repeat tab"),
    (S, "    totals_by = {p: ALL_BRANDS for p in brand_of} if everything else brand_of",
     "    totals_by = brand_of", "All brands has no overall total"),
    (T, "${ok ? pct(t.units_pct) : \"—\"}</div>", "${ok ? pct(t.repeat_pct) : \"—\"}</div>",
     "the card headline is the customer share"),
]

TESTS = [
    "tests/test_repeat_storage.py", "tests/test_repeat_keys.py", "tests/test_repeat_parse.py",
    "tests/test_repeat_fetch.py", "tests/test_repeat_logic.py", "tests/test_repeat_reference.py",
    "tests/test_repeat_api.py", "tests/test_repeat_refresh.py", "tests/test_repeat_page.py",
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
