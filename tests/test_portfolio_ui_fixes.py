"""Four defects a reverted UI refresh had fixed, re-fixed without the restyle.

A Materio-inspired token refresh was built across six commits and reverted in full
(`af82f1b`) — *"revert back from materio. I dont like it."* Its revert message listed the
defects it had fixed, and they all came back with the look. The owner's instruction this
time was **"fix what's broken, don't restyle"**, so these tests pin the fixes while
`tests/test_theme.py::test_the_theme_still_declares_no_new_tokens` pins that the restyle
has not returned.

Source-level assertions, for the reason `test_portfolio_screen.py` already states: these
are contracts with JavaScript that has no test runner.

**The sticky header is the one worth reading carefully, because the obvious fix is a
NON-FIX.** `.table-wrap` is `overflow-x:auto`, and per CSS Overflow setting one axis to
`auto` makes the other compute from `visible` to `auto` — so the wrapper was already a
scrollport on both axes and a sticky `thead` inside it positioned against THAT BOX rather
than the page. With no height constraint the box never scrolled, so the header had zero
travel. Measured in isolation under this page's exact conditions:

    scrollHeight 2415 == clientHeight 2415      the box cannot scroll
    thead th at -604px after 900px of scroll    the header leaves with the table

With a height cap, measured on the real page: 839px box, 345px of internal scroll, header
and footer both pinned at offset 1px throughout. So `position:sticky` and the cap are
**halves of one fix** and both are asserted — which is also why the reverted commit's
claim to have fixed this was untrue of this page.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.regression

TEMPLATE = Path(__file__).parent.parent / "templates" / "portfolio.html"


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _css(source: str) -> str:
    """The template's `<style>` block with comments stripped.

    Comments are stripped because the comments explaining these fixes quote the CSS they
    are about — so an unstripped scan finds the prose and reports the rule present when it
    is not, or absent when it is. That substring trap has bitten this codebase six times.
    """
    blocks = re.findall(r"<style>(.*?)</style>", source, re.S)
    return re.sub(r"/\*.*?\*/", " ", "\n".join(blocks), flags=re.S)


def _rule(css: str, selector: str) -> str:
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        if selector in match.group(1):
            return match.group(2)
    raise AssertionError(f"portfolio.html has no CSS rule for {selector!r}")


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    rest = source[start:]
    end = rest.find("\nfunction ", 1)
    return rest if end == -1 else rest[:end]


# ─── The sticky header, and why one half alone is a non-fix ───────────────────


def test_the_column_headings_stay_visible_while_the_rows_scroll():
    """**Both halves, because either alone leaves the header sliding off the page.**

    Over 90 products the headings scrolled away entirely — measured at -289px after 900px
    of scroll — so the owner was reading twelve columns of money with no labels.

    `position:sticky` without a height cap is the trap: the wrapper is already a scrollport
    (`overflow-x:auto` forces `overflow-y` to compute to `auto`), so the header positions
    against a box that never scrolls and therefore never sticks. That is precisely what the
    reverted refresh shipped.
    """
    css = _css(_source())
    wrap = _rule(css, ".table-wrap")
    assert "max-height" in wrap, (
        "the table has no height cap, so its wrapper never scrolls vertically and a sticky "
        "thead inside it has zero travel — the header slides off the page with the table"
    )
    assert "overflow-y" in wrap, (
        "vertical overflow is left to compute rather than declared; the computed value is "
        "the whole reason this bug was invisible, so it is written out deliberately"
    )
    head = _rule(css, "thead th")
    assert "position:sticky" in head.replace(" ", ""), "the column headings are not sticky"


def test_the_table_header_is_not_offset_into_the_first_row():
    """`top` must be 0, and this is the highest-value guard here.

    The offset is measured from `.table-wrap`, not the page, so the sticky page header's
    height is already accounted for by the wrapper's own position. ops.html's `top:56px`
    parked its header 56px DOWN INSIDE the table, directly over the first product row —
    hiding a row that was still in the DOM, which is the failure mode that looks like
    missing data rather than a layout bug.
    """
    head = _rule(_css(_source()), "thead th")
    tops = re.findall(r"(?:^|;)\s*top\s*:\s*([^;]+)", head)
    assert tops, "thead th has no `top`, so `position:sticky` has nothing to stick to"
    for value in tops:
        assert value.strip() in {"0", "0px"}, (
            f"thead th has top:{value.strip()} — any non-zero offset is measured from "
            "inside the table and parks the header on the first product row"
        )


def test_the_totals_row_is_pinned_ABOVE_the_headings_when_they_meet():
    """They meet on a short filtered grid, and there the summary should win.

    `tfoot` and `thead` only overlap when the table is shorter than the cap — a two-row
    filtered view — and the totals sitting *under* the headings there would hide the figure
    the row exists to show. Asserted as a RELATION, not as literals, so the ladder can be
    retuned without rewriting the test as a copy of the CSS.

    The tfoot's `position:sticky` was DECLARED long before this change and was inert,
    because `bottom:0` had no scroll region to stick to either.
    """
    css = _css(_source())
    def z(selector):
        found = re.search(r"z-index\s*:\s*(\d+)", _rule(css, selector))
        assert found, f"{selector} has no z-index"
        return int(found.group(1))

    header_z, thead_z, tfoot_z = z("header"), z("thead th"), z("tfoot tr.totals td")
    assert tfoot_z > thead_z, (
        f"the totals row (z={tfoot_z}) sits under the headings (z={thead_z}), so on a short "
        "filtered grid the figure it exists to show is hidden"
    )
    assert header_z > tfoot_z, (
        f"the page header (z={header_z}) must stay above the table's own sticky layers"
    )


def test_the_table_height_is_measured_rather_than_hardcoded():
    """A constant here is a constant assumption about every element above the table.

    ops.html shipped BOTH failure modes of exactly that constant: 90px of dead whitespace
    after the header above it was trimmed, and the last row hidden under a fixed bar on a
    short phone.

    **And the `max-height` VALUE must reference the variable**, not merely mention it
    somewhere in the file — ops.html's version of this test passed while its rule had been
    reverted to a hardcoded `calc()` and `sizeTable()` set a property nothing read.
    """
    source = _source()
    wrap = _rule(_css(source), ".table-wrap")
    cap = re.search(r"max-height\s*:\s*([^;]+)", wrap)
    assert cap, "the wrapper has no max-height"
    assert "--pf-table-cap" in cap.group(1), (
        f"max-height is {cap.group(1).strip()!r} — a hardcoded cap, so the measurement in "
        "sizeTable() is computed and then ignored"
    )

    body = _function(source, "sizeTable")
    assert "innerHeight" in body, "the cap is not measured against the viewport"
    assert "--pf-table-cap" in body, "sizeTable does not set the variable the CSS reads"
    assert 'addEventListener("resize", sizeTable)' in source, (
        "the cap is never re-measured, so rotating the tablet leaves it stale — the totals "
        "row then floats mid-screen or the last product is unreachable"
    )
    assert "sizeTable();" in _function(source, "renderTable"), (
        "the cap is not re-measured after a render, so a banner appearing or the filter "
        "builder opening leaves the box the wrong height"
    )


def test_the_cap_is_measured_against_the_VIEWPORT_not_the_chrome_above_it():
    """**Got this wrong first, and the measurement is what said so.**

    Sizing the box to the space left below everything above it — which is what ops.html
    does, correctly, because there the table IS the screen — left **77px** of table here:
    measured at 1440x900, this page carries 599px of legitimate chrome above the grid (the
    caveats line, the window bar, four KPI tiles, the category strip, the group tabs, the
    controls). It clamped to the 200px floor and showed four products.

    So the box is deliberately TALLER than the space initially free: the page scrolls the
    chrome away first, then the box takes over with its header pinned. Measured after:
    839px box, 345px of internal scroll.
    """
    body = _function(_source(), "sizeTable")
    assert "getBoundingClientRect" not in body or "wrap.getBoundingClientRect" not in body, (
        "the cap is sized from the WRAPPER's own top, which is the version that measured "
        "77px of table on a 900px viewport and clamped to the floor"
    )
    assert re.search(r"innerHeight\s*-", body), (
        "the cap is not derived from the viewport height"
    )
    # And a floor, so a 3-row filtered grid leaves no empty scroll well.
    assert "Math.max(" in body, "the cap has no floor"


def test_expanding_a_product_brings_its_NEW_rows_into_view():
    """The one regression the height cap introduces, and its mitigation.

    The grid now scrolls inside its own box, so expanding a product near the bottom opens
    its reason row and pack sizes below the fold — the rows are there and the click looks
    inert.

    **Scrolling the PARENT into view does not work**, measured: the parent is already on
    screen (that is how it got clicked), so `block:"nearest"` leaves it exactly where it
    was and all five children stayed hidden — parent at y=667 in a box ending at 797,
    children at 796-993, zero visible. Walking to the last new row and revealing THAT
    brings the whole group in: measured 5 of 5 visible, header and footer still pinned.
    """
    source = _source()
    handler = source[source.index('$("table-area").addEventListener("click"'):]
    handler = handler[: handler.index("\n});")]
    assert "scrollIntoView" in handler, (
        "expanding a product near the bottom of the capped box opens its rows out of sight"
    )
    assert 'block: "nearest"' in handler or 'block:"nearest"' in handler, (
        "use block:nearest so a group that already fits does not move at all"
    )
    assert 'classList.contains("parent")' in handler, (
        "the handler does not walk to the LAST new row, so it reveals the parent — which is "
        "already visible — and leaves the children below the fold"
    )


# ─── Tabular figures reach every money cell ──────────────────────────────────


def test_the_money_columns_are_all_tagged_num_in_all_three_render_functions():
    """**The likelier regression, and it is invisible where it would be reviewed.**

    The CSS rule is one line nobody touches; the render functions change every sprint. A
    cell that drops `class="num"` goes proportional — and measured, that drifts 4.45px on
    Arial and 2.22px on Roboto while staying perfect on the owner's Segoe UI box. So the
    column misaligns on the warehouse tablet and nowhere else.

    Scoped per function, because "this holds somewhere in 1,900 lines" is a different claim
    from "this function holds it" — the 4th-instance trap this codebase records.
    """
    source = _source()
    for name in ("dataCells", "detailCells", "totalsRow"):
        body = _function(source, name)
        found = body.count('class="num"')
        assert found == 8, (
            f"{name} emits {found} numeric cells, expected 8 — a money column has lost its "
            "`num` class and will render with proportional digits on the tablet"
        )


def test_the_rupee_and_the_minus_sign_are_NOT_treated_as_icons():
    """Typography, not iconography — the guard against over-applying the icon work.

    U+20B9 is the currency on every figure and U+2212 is the minus in the columns toggle.
    Replacing either with an SVG would be a drawing where a character belongs.
    """
    source = _source()
    assert "₹" in _function(source, "money"), (
        "the rupee sign has left money(), so every figure lost its currency"
    )
    assert "−" in _function(source, "renderColsButton"), (
        "the minus sign has left the columns toggle"
    )
