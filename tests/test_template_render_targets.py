"""Every element the page script renders into must exist in the markup.

Found the hard way, in a browser, after the whole suite was green.

Combined invoicing (`renderInvoiceBar`) was fully implemented and had five tests of its
own, all passing. It never appeared on the screen: `templates/shipment.html` had no
``<div id="invoice-bar">`` for it to draw into, and the function opens with

    const bar = document.getElementById("invoice-bar");
    if(!bar) return;

That guard is correct — a render function should not throw when its host is absent — and
it is exactly what made the bug silent. No console error, no failing test, no bar. Every
test asserted the *JavaScript* existed; none asserted it had anywhere to put its output.

This is the structural guard, applied to all templates rather than to the one that
happened to break, because the mistake is a property of the pattern and not of that file.

Deliberately narrow to avoid false alarms:

* Only ``getElementById`` — a CSS selector can legitimately match markup built at
  runtime, an id lookup for a container being *written to* cannot.
* Only when a write (``innerHTML`` / ``textContent`` / ``appendChild``) follows nearby,
  so reading an input's ``.value`` is not implicated.
* Ids created at runtime by the script itself are allowed, since they never appear as a
  literal ``id="..."`` attribute in the file.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.regression

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATES = sorted(p.name for p in TEMPLATE_DIR.glob("*.html"))

# A write through the looked-up reference has to appear within this many characters of
# the lookup. Long enough to span a guard clause and a blank line, short enough that an
# unrelated write further down the function is not attributed to it.
_WINDOW = 400

_WRITES = ("innerHTML", "textContent", "appendChild", "insertAdjacentHTML")


def _lookup_pattern(source: str) -> str:
    """Regex matching an id lookup, including a local ``$`` alias if one is defined.

    ``ops.html`` and ``users.html`` both do ``const $ = id => document.getElementById(id)``.
    Matching only the literal ``getElementById`` skipped both files entirely and would
    have skipped the very bug this test exists for had it landed in one of them.
    """
    alternatives = [r'getElementById\(\s*["\']([\w:-]+)["\']\s*\)']
    if re.search(r'const\s+\$\s*=\s*\w+\s*=>\s*document\.getElementById', source):
        alternatives.append(r'\$\(\s*["\']([\w:-]+)["\']\s*\)')
    return "|".join(alternatives)


def _render_targets(source: str) -> set[str]:
    """Ids fetched by id and then written to."""
    targets = set()
    for match in re.finditer(_lookup_pattern(source), source):
        name = next(g for g in match.groups() if g)
        window = source[match.end():match.end() + _WINDOW]
        if any(w in window for w in _WRITES):
            targets.add(name)
    return targets


def _declared_ids(source: str) -> set[str]:
    """Ids present as literal attributes, plus any the script builds itself.

    The second half matters: a script that generates ``id="row-${asin}"`` into a string
    is creating that element, so a lookup of a generated id is not a missing container.
    Templates that build ids from a variable are exempted wholesale rather than parsed.
    """
    declared = set(re.findall(r'id="([\w:-]+)"', source))
    declared |= set(re.findall(r"id='([\w:-]+)'", source))
    # `el.id = "foo"` / `setAttribute("id", "foo")`
    declared |= set(re.findall(r'\.id\s*=\s*["\']([\w:-]+)["\']', source))
    declared |= set(
        re.findall(r'setAttribute\(\s*["\']id["\']\s*,\s*["\']([\w:-]+)["\']', source)
    )
    return declared


def _builds_dynamic_ids(source: str) -> bool:
    """True when the file interpolates into an id, e.g. ``id="day-${date}"``."""
    return bool(re.search(r'id=["\'][^"\']*\$\{', source)) or bool(
        re.search(r'\.id\s*=\s*[`\'"][^`\'"]*\$\{', source)
    )


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_render_target_exists_in_the_markup(name):
    source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")

    targets = _render_targets(source)
    if not targets:
        pytest.skip(f"{name} renders into no looked-up container")

    declared = _declared_ids(source)
    missing = sorted(t for t in targets if t not in declared)

    if missing and _builds_dynamic_ids(source):
        # The file generates ids; a lookup may legitimately target one of those.
        # Only complain about targets that are clearly static container names.
        missing = [m for m in missing if not re.search(rf'id=["\'][^"\']*{re.escape(m)}', source)]

    assert not missing, (
        f"{name}: the script renders into element(s) that do not exist in the markup: "
        f"{missing}. A `if(!el) return;` guard makes this silent — no console error and "
        f"no failing test, the feature simply never appears on the page."
    )
