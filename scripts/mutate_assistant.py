"""Mutation harness for the Portfolio "Ask" assistant.

Run: ``venv/Scripts/python scripts/mutate_assistant.py``

Every mutation MUST be caught. No "All N mutations caught" line means the harness crashed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
T, S, B = "app/assistant/tools.py", "app/assistant/service.py", "app/assistant/bedrock.py"
R, P = "app/routers/assistant.py", "templates/_ask_panel.html"

MUTATIONS: list[tuple[str, str, str, str]] = [
    (T, '"net_pct": _pct(ratio(s["net"]))', '"net_pct": _pct(sum(float(p.get("net_pct") or 0) for p in parents) / max(1, len(parents)))',
     "a filtered total averages the rows' Net %"),
    (T, '            "weight_ordered_kg": _num(_known_sum(parents, "weight_ordered_kg"), 1),', '', "a total drops weight ordered"),
    (T, '    vals = [float(p[key]) for p in parents if p.get(key) is not None]', '    vals = [float(p.get(key) or 0) for p in parents]',
     "an unknown weight counts as 0 kg"),
    (T, '        **_sum_profit(by_cat.get(c["category"], [])),', '        **_sum_profit(data.get("parents") or []),',
     "a category carries the account's weight"),
    (T, '"net_per_kg": _num(s["net_weighed"] / net_kg) if net_kg else None}', '"net_per_kg": _num(s["net"] / net_kg) if net_kg else None}',
     "Net per kg divides by unweighed rows' net"),
    (T, "    return have + none", "    return none + have", "a missing figure sorts first"),
    (T, "    return max(1, min(MAX_LIMIT, n))", "    return max(1, n)", "the row limit is uncapped"),
    (T, '        if inp.get("category") and r["category"].casefold() != str(inp["category"]).casefold():\n            continue\n', "",
     "the profit category filter is ignored"),
    (T, "            if not retry.get(\"error\"):\n                out = retry", "            pass",
     "a named inactive product is reported as not found"),
    (T, '                inp["start"], inp["end"] = context["start"], context["end"]', "                pass",
     "the screen's window is ignored"),
    (T, '"acos": "no attributed sales" if p.get("acos_infinite") else _pct(p.get("acos")),',
     '"acos": _pct(p.get("acos")),', "spend with no attributed sales reads as a dash"),
    (T, "        c = ((r.get(\"w\") or {}).get(window) or {}) if available else {}",
     "        c = ((r.get(\"w\") or {}).get(window) or {})", "an unavailable window's figures leak"),
    (T, '"partial_fba_data": r.get("fba_share") is not None and r["fba_share"] < partial_below,',
     '"partial_fba_data": False,', "Easy-Ship-heavy rows are not flagged"),
    (T, "        total, scope = (cat or {}).get(\"total\"), (cat or {}).get(\"category\", inp[\"category\"])",
     "        pass", "a category scope reports the brand total"),
    (T, "    if len(found) > 1:\n        return {**_profit_header(data), \"several_match\"",
     "    if False:\n        return {**_profit_header(data), \"several_match\"", "an ambiguous name picks one silently"),
    (S, "    for turn in (history or [])[-HISTORY_TURNS:]:", "    for turn in (history or []):",
     "the whole history travels"),
    (S, '    system = [{"text": SYSTEM}, {"cachePoint": {"type": "default"}}]', '    system = [{"text": SYSTEM}]',
     "the system prompt is not cached"),
    (S, '            except Exception as exc:          # a tool bug must become an answer, not a 500\n                out = {"error": f"The tool failed: {exc}"}',
     '            except ZeroDivisionError as exc:\n                out = {"error": f"The tool failed: {exc}"}', "a tool bug becomes a 500"),
    (S, "                                           \"status\": \"error\" if out.get(\"error\") else \"success\"}})",
     "                                           \"status\": \"success\"}})", "a failed tool reads as success"),
    (S, '        usage["cache_read"] += int(u.get("cacheReadInputTokens") or 0)', "", "cache reads are not counted"),
    (S, "    return datetime.combine(ist.today(), dtime()) - ist.IST_OFFSET", "    return datetime.combine(ist.today(), dtime())",
     "the daily cap resets at UTC midnight, not IST"),
    (S, "    converse = converse or bedrock.converse\n", "", "converse bound at import"),
    (B, '    headers = {"Authorization": f"Bearer {settings.bedrock_api_key}"}', "    headers = {}", "no API key is sent"),
    (B, '        "additionalModelRequestFields": {"thinking": {"type": "between_tools"}},\n', "", "thinking left on"),
    (R, "    if await service.questions_today(db) >= settings.assistant_daily_limit:", "    if False:",
     "the daily limit is never applied"),
    (R, "    await service.log(db, username=username, tab=tab, question=question, result=result,\n                      started=started)",
     "    pass", "answers are not logged"),
    (R, "    context = {k: str(raw[k])[:80] for k in CONTEXT_KEYS if raw.get(k)}", "    context = dict(raw)",
     "any client field becomes context"),
    (P, "    return esc(s).replace(", "    return String(s).replace(", "the answer is rendered unescaped"),
    (P, "history: history.slice(-3).map(", "history: history.map(", "the whole history is sent"),
]

TESTS = ["tests/test_assistant.py"]


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
