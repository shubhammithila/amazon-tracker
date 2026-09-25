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


# ─── Icons: one drawing per icon, one home for every drawing ─────────────────

SPRITE = Path(__file__).parent.parent / "templates" / "_icon_sprite.html"
MACRO = Path(__file__).parent.parent / "templates" / "_icons.html"


def test_the_disclosure_caret_and_the_SORT_ARROW_are_the_same_chevron():
    """**The reported jitter: the first column changed width on every expand.**

    The caret was the characters ▸ and ▾, whose advance widths differ per operating system,
    so the Product column moved — and moved differently on the owner's Windows box than on
    the warehouse tablet. Measured after: the caret is a fixed **14px in both states** and
    rotates instead of swapping glyph. (The column does shift 455->436px on expand, fully
    reversibly, but that is table auto-layout redistributing once the size rows add content
    to other columns — not the caret.)

    One chevron symbol serves four arrows: collapsed, expanded, and both sort directions.
    """
    source = _source()
    stripped = re.sub(r"/\*.*?\*/|\{#.*?#\}|<!--.*?-->", " ", source, flags=re.S)
    for glyph in ("▸", "▾", "▲", "▼"):
        assert glyph not in stripped, (
            f"{glyph!r} is still rendered — a character whose width varies by OS, which is "
            "what made the first column jitter"
        )
    assert stripped.count('ico("chevron"') >= 3, (
        "the caret (two call sites) and the sort indicator should all be one chevron"
    )
    assert "ico-r90" in stripped, "the chevron never rotates, so expanded looks like collapsed"
    assert "aria-sort" in stripped, (
        "the sort DIRECTION must stay on aria-sort — the glyph never carried it, and an "
        "icon swap must not be the moment it is lost"
    )


def test_the_path_data_has_exactly_ONE_home():
    """The whole reason the sprite exists rather than a macro that emits paths.

    Measured: 11 of the 15 icon glyphs on this page were inside JavaScript template
    literals, which a Jinja macro cannot reach. Giving the JS its own copy of the geometry
    would be two homes for one drawing — the defect this codebase records shipping four
    times. Both halves emit a `<use>` reference and carry no `d=`.
    """
    # **Anchored on a word boundary, not the bare string `d="`.** Unanchored it matches
    # `id="`, `data-end="` and `aria-expanded="` — which is how the first version of this
    # test reported 40 false positives in a file containing no path data at all.
    path_attr = re.compile(r"(?<![-a-zA-Z])d=\"")
    assert path_attr.search(SPRITE.read_text(encoding="utf-8")), "the sprite holds no path data"
    for path in (TEMPLATE, MACRO):
        body = re.sub(r"/\*.*?\*/|\{#.*?#\}|<!--.*?-->", " ", path.read_text(encoding="utf-8"),
                      flags=re.S)
        assert not path_attr.search(body), (
            f"{path.name} carries its own path data — the drawing now has two homes and they "
            "will drift"
        )


def test_every_icon_REFERENCED_is_declared_in_the_sprite():
    """A `<use>` pointing at a missing symbol renders NOTHING — no error, no icon.

    Exactly the silent shape `test_template_render_targets.py` exists for: a typo in an id
    produces a blank space, an empty console and a passing test.
    """
    declared = set(re.findall(r'<symbol id="(i-[a-z-]+)"', SPRITE.read_text(encoding="utf-8")))
    assert declared, "the sprite declares no symbols"
    source = _source() + MACRO.read_text(encoding="utf-8")
    # Both call shapes: the Jinja macro's `#i-{{ name }}` and the JS helper's `#i-${name}`.
    used = set(re.findall(r'ico\("([a-z-]+)"', source)) | set(
        re.findall(r'icons\.icon\("([a-z-]+)"\)', source))
    missing = {f"i-{name}" for name in used} - declared
    assert not missing, (
        f"{missing} are referenced but not declared in the sprite, so they render as nothing"
    )


def test_no_icon_hardcodes_a_colour():
    """`currentColor` is what keeps the theme the only place colour lives.

    It also means an icon inside `.neg` is red and one inside `.dim` is dim with no per-icon
    rule — verified in the browser, where the first icon inherited the amber of the banner
    around it. And `tests/test_theme.py` fails any template with a hex or rgb() outside a
    comment, so a literal here would break that too.
    """
    sprite = SPRITE.read_text(encoding="utf-8")
    assert "currentColor" in sprite, "the icons do not inherit the surrounding text colour"
    body = re.sub(r"\{#.*?#\}", " ", sprite, flags=re.S)
    assert not re.findall(r"#[0-9a-fA-F]{6}\b", body), "an icon hardcodes a hex colour"
    assert not re.findall(r"rgba?\(", body), "an icon hardcodes an rgb colour"


def test_the_icons_are_decorative_and_hidden_from_a_SCREEN_READER():
    """Each sits beside its own words, so announcing it would be noise.

    Unconditional rather than a parameter, because a `label` argument is one a call site can
    forget — and the failure mode is silent for the person who cannot see the icon.
    """
    for path in (SPRITE, MACRO):
        assert 'aria-hidden="true"' in path.read_text(encoding="utf-8"), (
            f"{path.name} does not hide its decorative icons from assistive technology"
        )
    # `ico` is a `const` arrow function, so `_function` (which looks for `function name(`)
    # cannot find it — slice from the declaration instead.
    source = _source()
    helper = source[source.index("const ico = "):]
    helper = helper[: helper.index(";")]
    assert 'aria-hidden="true"' in helper, (
        "the JS icon helper omits aria-hidden, so 11 of the 15 icons announce themselves"
    )


def test_the_header_LOGO_is_left_as_it_is():
    """Branding, not a control — and ten other templates carry the same glyph in that slot.

    Converting one of eleven would create exactly the inconsistency this work removes.
    """
    header = _source()
    header = header[header.index("<header>"): header.index("</header>")]
    assert "📊" in header, (
        "the header logo was converted to an SVG, leaving this page's branding different "
        "from the other ten templates"
    )


def test_no_EMOJI_survives_as_an_icon():
    """**The whole inventory, not just the arrows** — a leftover glyph is the inconsistency.

    The chevron test above covers the four arrows because those caused the measured jitter. This
    one covers the rest, and it exists because a mutation swapping only the star back to `★`
    survived every other test here: the page would then mix an OS-dependent emoji with SVG icons,
    which is precisely what this work removes.

    **Two glyphs are deliberately KEPT and must not be converted:**

    * `📊` in the header is branding, and ten other templates carry the same glyph in that slot —
      converting one of eleven creates the inconsistency rather than removing it;
    * `₹` and `−` are typography. See `test_the_rupee_and_the_minus_sign_are_NOT_treated_as_icons`.

    Checked against RENDERED markup only, and that includes stripping JavaScript `//` line
    comments as well as the block forms — one of those still refers to "the ⚙ rules panel", which
    made the first version of this test fail on its own prose. Substring trap number seven.
    """
    rendered = re.sub(r"/\*.*?\*/|\{#.*?#\}|<!--.*?-->", " ", _source(), flags=re.S)
    # `//` comments only where they start a line or follow whitespace, so a `//` inside a URL or a
    # string literal is left alone.
    rendered = re.sub(r"(?m)(?<![:\w])//[^\n]*", " ", rendered)
    converted = {
        "⚙": "settings",      # gear
        "⚠": "alert",         # warning
        "★": "star",
        "ⓘ": "info",          # circled i
        "↻": "refresh",
        "⬇": "download",
        "▸": "chevron", "▾": "chevron",   # the disclosure pair
        "▲": "chevron", "▼": "chevron",   # the sort indicator
    }
    for glyph, name in converted.items():
        assert glyph not in rendered, (
            f"U+{ord(glyph):04X} is still rendered — it should be ico(\"{name}\"), or the page "
            "mixes an OS-dependent character with the SVG icons"
        )
    # ...and the ones that are meant to stay, so this test cannot be "fixed" by converting them.
    assert "\U0001f4ca" in rendered, "the header logo is branding and belongs to all 11 templates"
    assert "₹" in rendered, "the rupee sign is currency, not an icon"


# ── Only a PROBLEM gets a banner; a standing fact gets one collapsed line ──────
#
# Reported as *"too many messages at the top. dont want them if the logics are working fine"*,
# against a screenshot of FIVE stacked blocks — every one of them reporting CORRECT behaviour.
# That is the failure CLAUDE.md already records three times over: a caveat that fires on every
# render trains its reader to skip the one that matters, and the one that matters here is a
# failed refresh sitting underneath four paragraphs of routine.
#
# **These assert on INTERPOLATIONS INTO RENDERED STRINGS, not on `data.x` appearing in the
# function.** The first version checked the latter and SEVEN of eleven mutations walked straight
# through it — deleting the exclusion, dropping the named products, silencing the catalogue
# warning — because the flag was still *read* while nothing reached the screen. Eighth instance
# of that trap in this codebase, and the reason every helper below slices a REGION and looks for
# `${...}` inside a template literal.


def _banners(source: str) -> str:
    return _function(source, "renderBanners")


def _note_region(source: str) -> str:
    """Everything that feeds the collapsed  line — from the first `notes` to where it renders."""
    body = _banners(source)
    return body[body.index("const notes = ["): body.index('class="banner info caveats"')]


def _alert_region(source: str) -> str:
    """Everything after the collapsed line: the banners a real problem raises."""
    body = _banners(source)
    return body[body.index("Actual problems"):]


def _pushed_banners(region: str) -> str:
    """Only the banners actually handed to `out.push(...)` — i.e. only what reaches the screen.

    **This is the helper the whole block turns on, and getting it wrong let NINE mutations
    survive.** Each `out.push(` is taken to the start of the next one (or the end of the region),
    rather than to a matching backtick: these banners nest template literals, so a non-greedy
    `` `(.*?)` `` closes on an INNER backtick and truncates the payload — the same trap documented
    on `_in_note`.

    Two earlier versions were both too loose, and each let real mutations pass:

      1. `"data.catalogue_warning" in body` — true while the value was read in an `if` and
         rendered nowhere, so deleting the banner entirely passed.
      2. looking for `out.push` within 200 characters of the expression — which reaches BACKWARDS
         into the NEIGHBOURING banner's `out.push`, so replacing this one with `void(...)` passed.

    Ninth instance of the substring trap in this codebase.
    """
    chunks = []
    for match in re.finditer(r"out\.push\(", region):
        # Walk to the `)` that closes THIS call, tracking depth so a `${...}` or a nested call
        # cannot end the chunk early. Counting to the next `out.push(` instead made the final
        # chunk swallow the rest of the function — including the note-building code, which made
        # every collapsed fact look like a banner.
        depth, index = 0, match.end() - 1
        while index < len(region):
            if region[index] == "(":
                depth += 1
            elif region[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        chunks.append(region[match.start():index + 1])
    return "\n".join(chunks)


def _rendered(region: str, expression: str) -> bool:
    """Is `expression` interpolated into something that is PUSHED, not merely read?"""
    pushed = _pushed_banners(region)
    return any(f"${{{wrap}{expression}" in pushed for wrap in ("", "n(", "money(", "esc("))


def _in_note(region: str, expression: str) -> bool:
    """Is `expression` interpolated into text the collapsed note renders?

    Keyed on the `${...}` INTERPOLATION rather than on a template-literal boundary, and that is
    the third attempt at this helper — the two before it both let mutations through:

      1. `"data.x" in region` — true while the value was read in an `if` and rendered nowhere.
      2. capturing each `` `...` `` literal — **nested literals break it.** A conditional inside
         a literal, `${more > 0 ? ` and ${n(more)} more` : ""}`, makes a non-greedy backtick regex
         split at the INNER backticks, so `${n(more)}` lands in the GAP between two captures and
         is found in neither. Measured: 14 captures, with both `n(more)` and `n(x.sizes)` in gaps.

    `${` only appears inside a template literal in JavaScript, so searching for the interpolation
    directly needs no literal-boundary parsing at all — and it still fails for a value that is
    merely read, which is the distinction `_pushed_banners` draws on the other side.
    """
    return any(f"${{{wrap}{expression}" in region for wrap in ("", "n(", "money(", "esc("))


#: (expression that must be RENDERED in a banner, why it is a problem rather than a fact)
PROBLEM_BANNERS = [
    ("esc(lr.error)", "the figures shown are stale and nothing else on screen says why"),
    ("esc(data.catalogue_warning)", "the Active flags may be stale, so the wrong rows are hidden"),
    ("esc(data.unmatched_asins.join", "a product sold with no name — needs an MRP sheet edit"),
]


@pytest.mark.parametrize("expression,why", PROBLEM_BANNERS, ids=lambda v: v.split("(")[0][:18])
def test_a_real_problem_still_raises_its_own_banner(expression, why):
    """Quietening the routine must not quieten the breakage — the risk in this whole change.

    Fold a failed refresh into an  line and the screen goes silent about the one thing that
    needs acting on, which would be strictly worse than the five banners it replaced.
    """
    region = _alert_region(_source())
    assert _rendered(region, expression), (
        f"{expression} is not rendered into a pushed banner — {why}. Reading the value without "
        "pushing it leaves the screen silent about a real failure."
    )


def test_a_STALE_ratings_date_warns_while_a_FRESH_one_is_just_a_date_on_the_line():
    """The one item that is sometimes a fact and sometimes a problem, which is the whole split.

    Rule 6 splits BEST BET from SCALE on rating >= 4.0, so a stale rating silently shapes a
    verdict — and a stale rating is what revealed the product scrape had never run on
    production. **Never both**, or the quietening achieves nothing on the render that counts.
    """
    source = _source()
    assert "!data.ratings_stale" in _note_region(source), (
        "the collapsed line does not exclude the stale case, so a stale date would appear twice"
    )
    alerts = _alert_region(source)
    assert "data.ratings_stale" in alerts, "a stale ratings date no longer warns at all"
    stale = alerts[alerts.index("data.ratings_stale"):]
    assert 'out.push(`<div class="banner warn"' in stale[:400], (
        "a stale ratings date no longer raises a WARN banner"
    )


#: Standing facts — true on every visit, so each is a line in the collapsed note and NOT a banner.
#: Every entry names an expression that must still be INTERPOLATED, so deleting the information
#: fails here rather than passing as "successfully quietened".
COLLAPSED_FACTS = [
    ("data.inactive_hidden_skus", "how many sizes the Active flag excluded"),
    ("data.inactive_sales_units", "the excluded UNITS — the gap against a Business Report"),
    ("data.inactive_sales", "the excluded RUPEES; the KPI tiles are money, so this reconciles"),
    ("x.product", "the products are NAMED, because a wrong flag is a question only he can answer"),
    ("x.sizes", "a shown product that lost a pack size"),
    ("data.decided_but_inactive.join", "kept despite being inactive because a decision exists"),
]


@pytest.mark.parametrize("expression,why", COLLAPSED_FACTS, ids=lambda v: v.split(".")[-1][:24])
def test_a_standing_fact_is_COLLAPSED_and_not_DELETED(expression, why):
    """Both halves, and the second is what seven surviving mutations taught me to assert.

    "No longer a banner" is satisfied by deleting the information outright, which would trade
    five true-but-noisy blocks for a screen that quietly under-reports. So: it must still be
    rendered, and it must be rendered into the collapsed note rather than a banner of its own.
    """
    source = _source()
    assert _in_note(_note_region(source), expression), (
        f"{expression} is not rendered into the collapsed note — {why}. It was DELETED rather "
        "than collapsed, which under-reports instead of quietening."
    )
    # Scoped to the WHOLE function, not just the alert region. An earlier version checked only
    # the region after the collapsed line, so re-adding a standing banner ABOVE it — which is
    # exactly what a revert of this change looks like — walked straight through.
    assert not _rendered(_banners(source), expression), (
        f"{expression} still raises a standing banner — {why}. It is true on every render, so it "
        "belongs on the collapsed line."
    )


def test_renderBanners_can_push_ONLY_these_banners():
    """The set is the contract, and it is what a revert of this change would break.

    Every assertion above is about one datum. This one is about the SHAPE: five standing blocks
    became one collapsed line plus four conditions that mean something is wrong. A sixth banner
    appearing — for any reason, including a well-meant new caveat — is the drift this pins, and
    the per-datum tests cannot see it because they only ask about the data they name.

    The empty-grid pair is included because it genuinely IS a problem: no figures at all.
    """
    pushed = _pushed_banners(_banners(_source()))
    classes = re.findall(r'<div class="banner ([^"$]*)', pushed)
    kinds = {value.strip() for value in classes if value.strip()}
    assert kinds == {"warn", "info", "error", "info caveats"}, (
        f"the banner kinds changed: {sorted(kinds)}. A new standing banner is the drift that made "
        "the owner ask for this — five blocks of correct behaviour above the data."
    )
    # One collapsed line, and exactly one.
    assert classes.count("info caveats") == 1, (
        f"{classes.count('info caveats')} collapsed caveat lines — there must be exactly one, or "
        "the standing facts are back to competing for the top of the screen"
    )
    assert len(classes) <= 7, (
        f"renderBanners pushes {len(classes)} banners. Expected at most 7: the collapsed line, "
        "two empty-grid cases, and four problems (stale ratings, catalogue, unmatched, refresh)."
    )


def test_the_pre_COGS_caveat_survives_the_collapse():
    """The one fact here that stops a money-losing SKU reading as a keeper.

    Amazon's `netProceeds` is sales minus Amazon's fees minus ads and excludes what the product
    costs to make, so a size showing +8.8% may still lose money. That is why the caveat is also
    written into row 1 of the workbook — and why quietening the screen must not drop it.

    Asserted separately from the `COLLAPSED_FACTS` table because it is a literal string rather than
    an interpolated value, so `_in_note`'s `${...}` test cannot see it — which is exactly how
    deleting it survived nineteen mutations' worth of everything else.
    """
    note = _note_region(_source())
    assert "notes.push" in note[note.index("data.pre_cogs"):][:200], (
        "the pre-COGS caveat no longer reaches the collapsed note — a margin read as profit is how "
        "a money-losing SKU looks like a keeper"
    )
    assert "pre-COGS" in note, "the caveat's own text is gone"


def test_each_named_product_carries_its_OWN_units_and_rupees():
    """"White Sesame Seeds" alone is not actionable; "316u ₹50,027" is.

    The owner is being asked whether each Active flag is right, and the answer depends entirely on
    the SIZE of what it excluded — 316 units is probably a mis-set flag, 3 units is probably a
    deliberate run-down. A bare list of names, or a bare total, cannot support that judgement.

    Asserted on the PER-PRODUCT fields rather than the portfolio total, because two mutations
    survived a version that checked only `money(data.inactive_sales)`: that expression appears
    TWICE in the note — once on the collapsed line and once in the full text — so deleting either
    occurrence left the other satisfying the check. Same for `esc(x.product)`, which also appears
    in the sizes-of-shown line. The per-row figures appear exactly once each.
    """
    note = _note_region(_source())
    assert "${n(x.units)}u" in note, (
        "a named product no longer shows its own UNITS, so the owner cannot tell a mis-set flag "
        "from a deliberate run-down"
    )
    assert "${money(x.sales)}" in note, "a named product no longer shows its own sales"


def test_the_named_list_is_capped_while_its_COUNT_stays_exact():
    """"5 products" when 9 sold is a sentence the owner cannot act on.

    Naming all 56 inactive products would bury the 8 that matter among dead stock, so the list is
    capped — and the overflow then has to be stated, which is the same discipline the catalogue
    notes and the Projections `needs_review` list already follow.

    Scoped to the NOTE region deliberately: `more` is a generic local name and the empty-grid
    banner has its own `${n(more)}` for missing days, so a whole-function check confuses the two.
    That collision failed an earlier version of this test — a real finding about the assertion,
    not about the code.
    """
    note = _note_region(_source())
    assert "inactive_with_sales_count" in note, "the exact count is not read, so it cannot be stated"
    assert "${n(more)}" in note, (
        "the collapsed note does not state 'and N more', so a capped list reads as the whole list"
    )


def test_the_collapsed_line_states_the_hidden_SIZES_and_their_RUPEES_without_expanding():
    """The visible half has to carry enough that the  is a choice, not a requirement.

    A collapsed line reading only "3 notes" would make the exclusion invisible until clicked,
    which is how a 1.5% gap against a Business Report goes unnoticed — the "3,337 units against
    3,259" report. So the SIZE COUNT and the MONEY are on the line itself.
    """
    region = _note_region(_source())
    short = region[region.index("short: `${n(data.inactive_hidden_skus)}"):]
    short = short[: short.index("full:")]
    assert "hidden as inactive" in short
    assert "money(data.inactive_sales)" in short, (
        "the collapsed line does not state the excluded rupees, so the totals cannot be reconciled "
        "without expanding it"
    )
    # **And the EXPANDED text has to state the total too, independently.** Asserting only the
    # collapsed line let a mutation that stripped the total from the full text survive: the same
    # expression appears in both places, so one check covered whichever happened to remain. The
    # expanded text is where the owner reconciles against a Business Report, so the total belongs
    # there whatever the line says.
    expanded = region[: region.index("short: `${n(data.inactive_hidden_skus)}")]
    assert "money(data.inactive_sales)" in expanded, (
        "the EXPANDED note does not total the excluded rupees — the collapsed line alone leaves "
        "the named products with no sum to reconcile against"
    )
