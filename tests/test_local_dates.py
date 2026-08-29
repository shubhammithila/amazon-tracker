"""**`toISOString()` is a UTC formatter, and this business runs in IST (UTC+5:30).**

So a local `Date` rendered with it answers YESTERDAY between 00:00 and 05:30 — 5½ hours out of every
24, which is exactly when nobody is looking at the screen.

This is the fourth occurrence of one defect, and the first three were each fixed in isolation:

===================  =====================================================================
`orders.html`        ``new Date("2026-08-25")`` is UTC midnight by spec, so IST rendered the
                     ship-by date as **05:30 the following morning** — half a day into the
                     wrong day, in the column the warehouse plans against.
`ads.html`           ``maxDate()`` and ``presetRange()`` built a local date and formatted it
                     as UTC, so at 00:39 IST on 29 Aug the picker offered **27 Aug** as its
                     maximum.
`portfolio.html`     the same two functions, the same way, found when the page was next
                     opened. Three templates, one mistake, three separate fixes.
`invoice.html`       ``new Date().toISOString()`` on the **GST invoice date**. Simulated at
                     00:39 IST on 29 Aug it read **2026-08-28** — a tax document in a
                     legally-sequential series dated before the shipment it bills, which
                     nothing in the app can detect afterwards.
===================  =====================================================================

**Each per-template fix came with a per-template test, and the fourth still happened.** A guard
scoped to the file that broke cannot stop a habit that spans files, which is why this test exists at
the level the mistake actually lives at: every template, one rule.

The remedy is a shared `localDate(d)` — same name, same three local getters, in every template that
needs a date string. Stating the intent rather than encoding it also matters for the case that was
already right: `ops.html` shifted the instant by `getTimezoneOffset()` before formatting, which
worked, and is indistinguishable at a glance from the bug. Correct-but-unreadable is what gets copied
into the next template.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.regression

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATES = sorted(p.name for p in TEMPLATE_DIR.glob("*.html"))

#: The local getters a correct implementation must use.
LOCAL_GETTERS = ("getFullYear()", "getMonth()", "getDate()")


def _code(name: str) -> str:
    """A template's script content with comments stripped.

    Comments are removed because the fixes are DOCUMENTED by naming the call they replaced — an
    assertion that could not coexist with its own explanation would force the explanation out, and
    the explanation is the part that stops the fifth occurrence.
    """
    source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", source, flags=re.S)
    code = "\n".join(scripts)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"^\s*//.*$", "", code, flags=re.M)


def _function_body(code: str, name: str) -> str:
    """One function's body, by matching braces.

    Brace-matched rather than scanning for a newline plus ``}``: these templates indent their
    functions differently (`invoice.html` nests everything two spaces in), and an extractor keyed on
    a column-0 closing brace raised `ValueError` there — a test that ERRORS instead of failing tells
    you nothing about the code it was checking.
    """
    start = code.index(f"function {name}(")
    depth = 0
    for i in range(code.index("{", start), len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    raise AssertionError(f"{name} has no closing brace")


@pytest.mark.parametrize("template", TEMPLATES)
def test_no_template_formats_a_date_through_utc(template):
    """The rule, applied to every template rather than to the ones that have broken so far.

    Deliberately a blanket ban rather than a check for the offset-shifting workaround. Both
    `d.toISOString()` and `new Date(d - offset).toISOString()` appear in this codebase's history, one
    wrong and one right, and telling them apart needs a reader to reconstruct the timezone
    arithmetic. A helper that says what it means is cheap; a subtly-correct idiom is not.
    """
    code = _code(template)
    assert "toISOString" not in code, (
        f"{template} formats a date through UTC, which renders the previous day for 5.5 hours out "
        f"of every 24 in IST — use localDate() instead"
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_a_local_date_helper_is_built_from_the_local_getters(template):
    """Where `localDate` exists, it must be the real thing.

    Without this, satisfying the ban above is as easy as renaming the call — the function would pass
    a grep for "localDate" while still going through UTC inside.
    """
    code = _code(template)
    if "function localDate(" not in code:
        pytest.skip(f"{template} needs no date string")
    body = _function_body(code, "localDate")
    for getter in LOCAL_GETTERS:
        assert getter in body, (
            f"{template}: localDate does not use {getter}, so it is not reading local time"
        )


def test_every_template_that_needs_a_date_string_has_the_helper():
    """The four templates that set a date input, named, so removing the helper is a failure.

    Listed explicitly rather than detected: `type="date"` also appears on inputs the user fills in,
    and the claim being made is about the four places the APP chooses a date — three pickers and one
    GST invoice date.
    """
    for template in ("ads.html", "portfolio.html", "invoice.html", "ops.html"):
        code = _code(template)
        assert "function localDate(" in code, (
            f"{template} sets a date but has no localDate helper"
        )


def test_the_invoice_date_is_local_because_it_reaches_a_tax_document():
    """Singled out because the consequence is different in kind, not degree.

    A wrong date on a picker is corrected by the person looking at it. A wrong date on a GST invoice
    is spent: the number comes from a legally-sequential series, the document is filed, and nothing
    in the app compares it against the shipment afterwards. The bug back-dated it by a day for the
    5.5 hours in which the day's last invoice is most likely to be raised.
    """
    code = _code("invoice.html")
    assert 'getElementById("f-date").value = localDate(' in code, (
        "the invoice date is not built from local time"
    )


def test_a_date_string_is_never_parsed_back_through_the_date_constructor():
    """The Orders-tab half of the same defect, which the ban above does not cover.

    `new Date("2026-08-25")` is UTC midnight **by spec**, so rendering it with any IST time formatter
    gives 05:30 the next morning. That shipped, in the ship-by column the warehouse plans against.
    Asserted where a date-only string is involved: `dateIST` is the helper that splits the string
    instead, and a test on orders.html already pins it — this generalises the check to every
    template, since the string shape is the same everywhere it appears.
    """
    for template in TEMPLATES:
        code = _code(template)
        # A `new Date(...)` whose argument is a bare YYYY-MM-DD literal or a variable holding one.
        # Narrow on purpose: `new Date()` with no argument, and with a numeric timestamp, are both
        # unambiguous and fine.
        bad = re.findall(r'new Date\(\s*["\']\d{4}-\d{2}-\d{2}["\']\s*\)', code)
        assert not bad, (
            f"{template}: {bad} — a date-only string is parsed as UTC midnight, which renders as "
            f"05:30 the NEXT day in IST"
        )
