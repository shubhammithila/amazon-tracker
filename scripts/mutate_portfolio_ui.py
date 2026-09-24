"""Mutation harness for the four UI defect fixes: sticky headings, tabular figures, the nav, icons.

Run: ``venv/Scripts/python scripts/mutate_portfolio_ui.py``

Every mutation MUST be caught. Each breaks ONE decision and names the test that has to notice;
a survivor means the test asserts a conclusion rather than the reason for it.

**Two mutations here are the reason this harness exists, and they are the dangerous kind:
removing the height cap, or dropping `overflow-y`, makes the sticky header SILENTLY INERT while
the CSS still reads `position:sticky`.** That is not hypothetical — it is precisely what the
reverted Materio refresh shipped, with a commit message claiming verification that was true of
`ops.html` (which caps its height) and impossible on this page. Measured under Portfolio's exact
conditions: scrollHeight 2415 == clientHeight 2415, and the header at -604px after 900px of
scroll.

Several mutations RESTORE the pre-fix behaviour, because that behaviour passed 2,388 tests.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

THEME = "static/theme.css"
PF = "templates/portfolio.html"
SPRITE = "templates/_icon_sprite.html"
TESTS_FILE = "tests/test_theme.py"

#: (file, find, replace, why this would be a real bug)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── The sticky headings: both halves of one fix ──
    (
        PF,
        "  max-height:var(--pf-table-cap, calc(100vh - 60px));min-height:200px;",
        "  min-height:200px;",
        "**the height cap goes, so the wrapper never scrolls and the sticky header has ZERO "
        "TRAVEL — silently inert while the CSS still says `position:sticky`.** The page looks "
        "exactly as it does today, which is how the reverted refresh shipped this "
        "— test_the_column_headings_stay_visible_while_the_rows_scroll",
    ),
    (
        PF,
        ".table-wrap{overflow-x:auto;overflow-y:auto;",
        ".table-wrap{overflow-x:auto;",
        "the declaration that looks redundant is load-bearing: without it the vertical overflow "
        "is left to COMPUTE, which is the trap that made this bug invisible for a year "
        "— test_the_column_headings_stay_visible_while_the_rows_scroll",
    ),
    (
        PF,
        "border-bottom:2px solid var(--border-strong);position:sticky;top:0;z-index:2}",
        "border-bottom:2px solid var(--border-strong);position:sticky;top:56px;z-index:2}",
        "the header parks 56px DOWN INSIDE the table, directly over the first product row — "
        "which stays in the DOM and invisible, so it reads as missing data rather than a layout "
        "bug. ops.html shipped exactly this — test_the_table_header_is_not_offset_into_the_first_row",
    ),
    (
        PF,
        "border-bottom:2px solid var(--border-strong);position:sticky;top:0;z-index:2}",
        "border-bottom:2px solid var(--border-strong)}",
        "the headings are not sticky at all, which is the reported defect "
        "— test_the_column_headings_stay_visible_while_the_rows_scroll",
    ),
    (
        PF,
        "position:sticky;bottom:0;z-index:3}",
        "position:sticky;bottom:0;z-index:1}",
        "on a short filtered grid the totals row slides UNDER the headings, hiding the figure it "
        "exists to show — test_the_totals_row_is_pinned_ABOVE_the_headings_when_they_meet",
    ),
    (
        PF,
        "max-height:var(--pf-table-cap, calc(100vh - 60px))",
        "max-height:calc(100vh - 300px)",
        "the cap is hardcoded, so `sizeTable()` measures and the CSS ignores it — ops.html's own "
        "version of this test passed in exactly that state "
        "— test_the_table_height_is_measured_rather_than_hardcoded",
    ),
    (
        PF,
        'window.addEventListener("resize", sizeTable);',
        "",
        "rotating the tablet leaves a stale cap: the totals row floats mid-screen or the last "
        "product is unreachable — test_the_table_height_is_measured_rather_than_hardcoded",
    ),
    (
        PF,
        "  sizeTable();\n}",
        "}",
        "the cap is never re-measured after a render, so a banner appearing or the filter builder "
        "opening leaves the box the wrong height "
        "— test_the_table_height_is_measured_rather_than_hardcoded",
    ),
    (
        PF,
        '      let last = again, next = again.nextElementSibling;\n'
        '      while(next && !next.classList.contains("parent")){ last = next; next = next.nextElementSibling; }\n'
        '      last.scrollIntoView({block: "nearest"});',
        '      again.scrollIntoView({block: "nearest"});',
        "**the version that does NOT work**, and it looks right: revealing the PARENT leaves it "
        "exactly where it was (it is on screen — that is how it got clicked), so all five new "
        "rows stay below the fold. Measured: parent at y=667 in a box ending at 797, children at "
        "796-993, zero visible — test_expanding_a_product_brings_its_NEW_rows_into_view",
    ),

    # ── Tabular figures ──
    (
        THEME,
        "th.num, td.num, .kpi .v { font-variant-numeric: tabular-nums; }",
        "",
        "money columns go back to proportional digits: 4.45px of drift on Arial and 2.22px on "
        "Roboto, and 0 on the owner's Segoe UI — so the columns misalign on the warehouse tablet "
        "and nowhere he would see it — test_numeric_cells_use_tabular_figures",
    ),
    (
        THEME,
        "th.num, td.num, .kpi .v { font-variant-numeric: tabular-nums; }",
        ".kpi-value { font-variant-numeric: tabular-nums; }",
        "**the reverted refresh's own mistake**: `.kpi-value` matches NO element in this codebase, "
        "so the property is 'applied' in the file and absent from the screen "
        "— test_numeric_cells_use_tabular_figures",
    ),
    (
        THEME,
        "th.num, td.num, .kpi .v { font-variant-numeric: tabular-nums; }",
        "td { font-variant-numeric: tabular-nums; }",
        "tabular digits reach PROSE — product names, verdict reasons, the merchant/FBA note — "
        "which is the numbers-versus-prose split `.chan` already makes, inverted "
        "— test_numeric_cells_use_tabular_figures",
    ),
    (
        PF,
        '    <td class="num">${kg(row.weight_kg)}</td>',
        "    <td>${kg(row.weight_kg)}</td>",
        "ONE of the three render functions loses the class, so the weight column goes proportional "
        "on the tablet only — invisible on the box where it would be reviewed "
        "— test_the_money_columns_are_all_tagged_num_in_all_three_render_functions",
    ),

    # ── The nav ──
    (
        THEME,
        ".nav-links { flex-wrap: wrap; row-gap: 4px; }",
        ".nav-links { row-gap: 4px; }",
        "Logout is unreachable again on all eight pages: measured at x=800 on a 375px screen, with "
        "426px of document sideways scroll "
        "— test_the_nav_wraps_rather_than_overflowing_a_phone",
    ),
    (
        THEME,
        "  flex-wrap: wrap;\n}\n\n/* **The nav overflowed",
        "}\n\n/* **The nav overflowed",
        "the header cannot wrap, so it squeezes the nav to 138px and stacks nine links into FIVE "
        "rows — the half of this fix that was found by measuring rather than reasoning "
        "— test_the_nav_wraps_rather_than_overflowing_a_phone",
    ),
    (
        THEME,
        "  --radius: 8px;",
        "  --radius: 8px;\n  --fs-md: 13px;",
        "**the reverted Materio type scale returns one token at a time** — the creep this guard "
        "exists to stop — test_the_theme_still_declares_no_new_tokens",
    ),

    # ── Icons ──
    (
        PF,
        '<span class="caret">${ico("chevron", isOpen ? "ico-r90" : "")}</span>',
        '<span class="caret">${isOpen ? "▾" : "▸"}</span>',
        "the reported jitter returns: two characters of different advance width per OS, so the "
        "Product column changes size on every expand — and this is exactly what 'revert just that "
        "bit' would produce — test_the_disclosure_caret_and_the_SORT_ARROW_are_the_same_chevron",
    ),
    (
        PF,
        'const ico = (name, extra) =>\n'
        '  `<svg class="ico${extra ? " " + extra : ""}" aria-hidden="true" focusable="false">'
        '<use href="#i-${name}"/></svg>`;',
        'const ico = (name, extra) =>\n'
        '  `<svg class="ico${extra ? " " + extra : ""}" focusable="false">'
        '<use href="#i-${name}"/></svg>`;',
        "11 of the 15 icons start announcing themselves to a screen reader, beside the words they "
        "already duplicate — test_the_icons_are_decorative_and_hidden_from_a_SCREEN_READER",
    ),
    (
        PF,
        'ico("chevron", sort.dir < 0 ? "ico-r90" : "ico-r270")',
        'ico("chevrn", sort.dir < 0 ? "ico-r90" : "ico-r270")',
        "a `<use>` at a missing symbol renders NOTHING — no console error, no icon, no failing "
        "test. The silent shape `test_template_render_targets.py` exists for "
        "— test_every_icon_REFERENCED_is_declared_in_the_sprite",
    ),
    (
        SPRITE,
        '<symbol id="i-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor"',
        '<symbol id="i-chevron" viewBox="0 0 24 24" fill="none" stroke="#55606f"',
        "an icon hardcodes a colour, breaking `.neg`/`.dim` inheritance AND the theme-wide ban "
        "that keeps colour in one file — test_no_icon_hardcodes_a_colour",
    ),
    # A `flex:none` mutation was here and is DELETED rather than paired with a test.
    #
    # Its stated rationale was that an icon inside `.controls` or `.decide` would be squashed,
    # since an SVG has no intrinsic ratio to defend itself with. **Measured in a browser: 0 of
    # the 44 icons on this page sit inside a flex parent** — every one is in a `display:block`
    # or inline context, and forcing `flex: 1 1 auto` left the width at 13px either way.
    #
    # So `flex:none` is DEFENSIVE, not load-bearing, and a test asserting it would pin an
    # incidental detail rather than a behaviour — which is the trap this codebase records four
    # times. The declaration stays in the CSS (it costs nothing and the next icon may well go
    # in a flex row); the claim that its removal is a bug does not.
    (
        PF,
        'return `<span class="${cls}">${rating.toFixed(1)}${ico("star")}',
        'return `<span class="${cls}">${rating.toFixed(1)}★',
        "one glyph is left behind, so the page mixes an OS-dependent emoji star with SVG icons — "
        "the inconsistency this work exists to remove "
        "— test_no_EMOJI_survives_as_an_icon",
    ),
    (
        TESTS_FILE,
        'FRAGMENTS = {"nav.html", "_icons.html", "_icon_sprite.html"}',
        'FRAGMENTS = {"nav.html"}',
        "proves the exemption is load-bearing rather than cargo-culted: the two partials have no "
        "<head>, so the suite must FAIL when they are held to the stylesheet-link rule",
    ),
]

TESTS = [
    "tests/test_portfolio_ui_fixes.py",
    "tests/test_theme.py",
    "tests/test_portfolio_screen.py",
    "tests/test_portfolio_api.py",
    "tests/test_nav_consistency.py",
]


def run_tests() -> bool:
    proc = subprocess.run(
        [str(ROOT / "venv/Scripts/python"), "-m", "pytest", "-q", "-x", "-p", "no:randomly", *TESTS],
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
