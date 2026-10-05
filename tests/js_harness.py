"""Run pieces of templates/portfolio.html under Node, so render output is TESTED rather than grepped.

Extraction is by NAME: top-level `function NAME(` blocks and `const NAME = ` statements are copied
verbatim from the template. Only the DOM and `data` are faked. A stub for one of the page's own
helpers is exactly how a probe once reported a phantom banner — so helpers are copied, never written.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "templates" / "portfolio.html"

#: Everything the column builders and the panel read, copied verbatim from the template.
FUNCTIONS = [
    "esc", "stars", "money", "pct", "kg", "acosCell", "verdictClass", "groupFlag", "sizeName",
    "normaliseLayout", "visibleColumns", "cell", "tableMinWidth", "headerHtml", "dataCells",
    "detailCells", "computeTotals", "totalsRow", "sizeRowHtml",
]
#: The Columns-panel logic, opted into by the panel tests (`run_portfolio_js(..., panel=True)`).
PANEL_FUNCTIONS = ["columnsPanelHtml", "applyLayout", "moveColumn", "setHidden"]
CONSTS = ["n", "ico", "COLUMN_DEFS", "FALLBACK_ORDER", "FIELDS"]

#: Statements that end a top-level function when a script is sliced by `function NAME(`.
_TOP_LEVEL = ("\nconst ", "\nlet ", "\n$(", "\ndocument.", "\nload()", "\n/*", "\n//")


def _script() -> str:
    source = TEMPLATE.read_text(encoding="utf-8")
    return source[source.rindex("<script>") + len("<script>"): source.rindex("</script>")]


def _function(script: str, name: str) -> str:
    start = script.index(f"function {name}(")
    rest = script[start:]
    end = rest.find("\nfunction ", 1)
    if end == -1:
        end = rest.find("\nasync function ", 1)
    block = rest if end == -1 else rest[:end]
    cuts = [block.find(m) for m in _TOP_LEVEL if block.find(m) != -1]
    return block[: min(cuts)] if cuts else block


def _const(script: str, name: str) -> str:
    """`const NAME = …;` up to the first `;` at bracket depth 0 and OUTSIDE any string.

    String-aware on purpose: `ico` is a multi-line arrow returning a template literal, and a `;`
    inside a string or `${…}` must not end the statement. Every const extracted here ends with `;`.
    """
    start = script.index(f"const {name} = ")
    depth, i, quote, stack = 0, start, None, []
    while i < len(script):
        ch = script[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if quote == "`" and script.startswith("${", i):
                stack.append(depth)
                depth = 0
                quote = None
                i += 2
                continue
            if ch == quote:
                quote = None
        # Comments are skipped whole: an apostrophe in "the parent's number" inside a `//` comment
        # otherwise opens a string and the statement runs on past its `;` — found by the first run.
        elif script.startswith("//", i):
            i = script.index("\n", i)
            continue
        elif script.startswith("/*", i):
            i = script.index("*/", i) + 2
            continue
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if ch == "}" and depth == 0 and stack:
                depth = stack.pop()
                quote = "`"
            else:
                depth -= 1
        elif ch == ";" and depth == 0 and not stack:
            return script[start:i + 1]
        i += 1
    raise AssertionError(f"could not extract const {name}")


def run_portfolio_js(body: str, *, panel: bool = False):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the render tests need it")
    script = _script()
    parts = [
        "let data = {}; let sort = {key: 'sales', dir: -1}; let layout = null;",
        "const $ = () => null;",
        "function emit(v){ console.log(JSON.stringify(v)); }",
    ]
    parts += [_const(script, name) for name in CONSTS]
    parts += [_function(script, name) for name in FUNCTIONS + (PANEL_FUNCTIONS if panel else [])]
    parts.append(body)
    path = os.path.join(tempfile.gettempdir(), "pf_render_test.js")
    Path(path).write_text("\n".join(parts), encoding="utf-8")
    result = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])
