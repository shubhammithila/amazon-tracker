"""Mutation harness for the Portfolio column picker (hide + reorder, saved per login).

Run: ``venv/Scripts/python scripts/mutate_portfolio_columns.py``

Every mutation MUST be caught. Each breaks ONE decision; a survivor means a test asserts a conclusion
rather than the reason for it. Mutations DELETE or REPLACE code, never dead-code a guard.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
T, COLS, ROUTER = "templates/portfolio.html", "app/portfolio/columns.py", "app/routers/portfolio.py"

MUTATIONS: list[tuple[str, str, str, str]] = [
    (T, '    ${visibleColumns().slice(1).map(c => cell(c, c.total(t))).join("")}',
     '    ${normaliseLayout(null, data.columns).order.map(id => cell(Object.assign({id}, COLUMN_DEFS[id]), COLUMN_DEFS[id].total(t))).join("")}',
     "the totals row ignores the saved order and hidden set"),
    (T, 'return visibleColumns().slice(1).map(c => cell(c, c.detail(row))).join("");',
     'return normaliseLayout(null, data.columns).order.map(id => cell(Object.assign({id}, COLUMN_DEFS[id]), COLUMN_DEFS[id].detail(row))).join("");',
     "size and flavour rows ignore the layout"),
    (T, "    current.order.filter(id => !current.hidden.includes(id) && COLUMN_DEFS[id])",
     "    current.order.filter(id => COLUMN_DEFS[id])",
     "hidden columns are still rendered"),
    (COLS, '    {"id": "sales",       "label": "Sales (ex-GST)", "locked": True},',
     '    {"id": "sales",       "label": "Sales (ex-GST)", "locked": False},',
     "a protected column becomes hideable"),
    (COLS, "        clean.insert(at, col)", "        pass",
     "a NEW column silently never appears for users who customised"),
    (COLS, "        if col in known and col not in _LOCKED and col not in hide:",
     "        if col in known and col not in hide:",
     "the server normaliser lets a protected column be hidden"),
    (T, "    clean.splice(pred.length ? clean.indexOf(pred[pred.length - 1]) + 1 : 0, 0, id);",
     "    clean.push(id);",
     "the CLIENT twin disagrees with the server about where a missing column goes"),
    (ROUTER, '    username = get_current_username(request)\n    if not username:\n'
             '        return JSONResponse(\n            {"error": "Shared-password',
     '    username = (await request.json()).get("username") or get_current_username(request)\n'
     '    if not username:\n        return JSONResponse(\n            {"error": "Shared-password',
     "PUT takes the account from the BODY — anyone can overwrite anyone's layout"),
    ("app/users.py", "    data[key] = value\n    user.preferences_json = json.dumps(data)",
     "    data = {key: value}\n    user.preferences_json = json.dumps(data)",
     "saving one screen's preference wipes every other screen's"),
    (ROUTER, '        "column_scope": "account" if username else "browser",',
     '        "column_scope": "account",',
     "a shared-password session is told to save to an account it does not have"),
    (T, '    sort = {key: "sales", dir: -1};\n    remember("sort", sort);',
     '    remember("sort", sort);',
     "sorting by a hidden column is not reset"),
    (T, '<table style="min-width:${tableMinWidth()}px">', '<table style="min-width:1120px">',
     "the table floor reverts to a literal and pads a narrow layout"),
    (T, '  return `<td data-col="${col.id}"${cls', '  return `<td${cls',
     "cells lose their column id, so alignment can only be checked by count"),
    (T, '  if(i < 0 || j < 0 || j >= order.length) return;', '  if(i < 0) return;',
     "moving past either end corrupts the order"),
    (T, '${locked.has(id) ? " disabled" : ""}>', '>',
     "a protected column's tick box is clickable in the panel"),
    ("deploy/update-ec2.sh",
     'elif "preferences_json" in cols("users"):\n'
     '    print("c3d8e1f5a702")                           # head: per-login display preferences\n',
     "", "the baseline detector is stale and stamps production BACKWARDS"),
]

TESTS = [
    "tests/test_portfolio_columns.py",
    "tests/test_portfolio_columns_render.py",
    "tests/test_schema_migrations.py",
    # Holds the check that the <table> USES tableMinWidth(); mutation 12 survived without it.
    "tests/test_portfolio_api.py",
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
