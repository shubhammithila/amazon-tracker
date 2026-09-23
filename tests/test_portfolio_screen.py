"""The Portfolio screen: the totals row, the flavour nesting, and two bugs found the same day.

Assertions on `templates/portfolio.html` rather than on rendered output, in the style
`tests/test_portfolio_api.py` already established for this page. The reason is stated there: these
ids and rules are contracts with the JavaScript, and the JavaScript has no test runner.

Three of the four things pinned here were invisible to every existing test:

* the **units that "were not showing"** on child rows were rendered correctly all along and pushed
  off the right edge of the scroll wrapper by a `nowrap` sentence in the first column;
* `toISOString()` shifted the date picker a day early for 5½ hours out of every 24;
* the totals row is new, so nothing was guarding the one rule that matters about it — that a
  percentage is recomputed from the sums and never averaged.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.regression


def _template() -> str:
    return (Path(__file__).parent.parent / "templates" / "portfolio.html").read_text(
        encoding="utf-8"
    )


def _function(source: str, name: str) -> str:
    """The body of one top-level function, up to the next one.

    Scoped rather than searching the whole file, because "this rule is followed somewhere in 1,300
    lines" is not the same claim as "this function follows it".
    """
    start = source.index(f"function {name}(")
    rest = source[start:]
    end = rest.find("\nfunction ", 1)
    return rest if end == -1 else rest[:end]


# ─── The totals row ──────────────────────────────────────────────────────────


def test_the_totals_row_is_a_tfoot_and_not_another_data_row():
    """Asked for as "show total/average of all columns".

    A real `tfoot` rather than a last `tbody` row, for two reasons that are not cosmetic:

    * **Sorting reorders the tbody.** A total inside it could drift into the middle of the list,
      where it reads as one product carrying the sales of the whole account.
    * It is a SUMMARY of the column rather than a member of it, and the element is what says so to
      a screen reader — which matters more here than usual, because its percentages are recomputed
      rather than summed, and a reader announcing it as data would invite adding it to the rows
      above.
    """
    source = _template()
    assert "function totalsRow(" in source, "there is no totals row"
    assert "<tfoot>${totalsRow(" in source, (
        "the totals row is not in a tfoot, so sorting could move it into the middle of the data"
    )


def test_the_totals_row_recomputes_percentages_and_never_averages_them():
    """**The mean of 90 products' TACOS belongs to no product.**

    It weights a size that sold 1 unit equally with one that sold 400 — the same error
    `logic._sum_sizes` exists to avoid on the server, and it looks entirely plausible in a summary
    row. So money and units sum, and every ratio is recomputed from those sums.

    Rating is the ONE genuine average, and it is weighted by review count: a 5.0 from 2 reviews is
    not evidence equal to a 3.8 from 400. Measured on the live window, the plain mean is 3.86
    against a review-weighted 3.95.
    """
    body = _function(_template(), "totalsRow")

    assert "ratio(spend, sales)" in body, "TACOS is not recomputed from the summed money"
    assert "ratio(net, sales)" in body, "net % is not recomputed from the summed money"
    assert "ratio(refunded, ordered)" in body, "returns % is not recomputed from the summed units"
    assert "ratio(adsCost, attributed)" in body, "ACOS is not recomputed from the summed money"

    # The tell-tale of an average is dividing by how many ROWS there are. It is legitimate only for
    # the rating, which divides by total REVIEWS — so the row count must appear in no arithmetic.
    assert "/ list.length" not in body and "/list.length" not in body, (
        "a column is divided by the row count, which is an average of ratios"
    )
    assert "r.rating * n(r.rating_count)" in body, "the rating average is not review-weighted"

    # A ratio with no denominator is still a dash, never 0% — the whole-table form of the rule
    # `logic._ratio` follows, and for the same reason: 0% TACOS reads as perfect efficiency.
    assert "whole ? part / whole : null" in body, (
        "a zero denominator would render 0% or NaN rather than a dash"
    )


def test_the_totals_row_covers_the_filtered_rows_not_the_whole_account():
    """It sits beneath a grid that a verdict chip, custom filters and a search box can each narrow.

    A constant account total there would silently answer a different question from the rows above
    it — the "86 orders beside 87 lines" defect this codebase has already been bitten by. So it is
    computed from the rendered list, and it says how many rows it covers.
    """
    source = _template()
    assert "totalsRow(list, isSkus)" in source, "the totals row is not given the filtered list"

    # `list` is the FILTERED set; `rows()` is every row for the grain. Passing the latter compiles,
    # renders, and is wrong — a mutation swapping them survived an earlier version of this test,
    # which only checked that `data.totals` was not read. Both wrong sources are now named.
    assert "totalsRow(rows()" not in source, (
        "the totals row is given every row for the grain rather than the filtered list, so it "
        "would contradict the rows above it whenever a filter is active"
    )

    body = _function(source, "totalsRow")
    assert "data.totals" not in body, (
        "the totals row reads the unfiltered account totals, so it would contradict the rows "
        "above it whenever a filter is active"
    )
    assert "rows()" not in body, (
        "the totals row reaches past its argument to the unfiltered rows"
    )
    assert "list.length" in body, "the row does not say how many rows it is totalling"


def test_the_totals_row_has_one_cell_per_column():
    """Twelve columns, so twelve cells — a short row silently shifts every figure left.

    Counted rather than eyeballed: a totals row misaligned by one column would put ad spend under
    TACOS and still look like a plausible table.

    **The literal was 11**, and moved when Units came out from behind the "+ More columns" toggle and
    Weight was added beside it. Deliberately NOT derived from the `COLUMNS` array: this test exists
    to catch the footer and the header disagreeing, so counting the same list both are meant to
    follow would make it self-fulfilling.
    """
    body = _function(_template(), "totalsRow")
    cells = body.count("<td")
    assert cells == 12, f"the totals row has {cells} cells for 12 columns"


# ─── Why the child rows appeared to have no units ────────────────────────────


def test_the_channel_note_wraps_so_the_number_columns_stay_on_screen():
    """**This is why the child SKU rows appeared to have no units.**

    `tbody td` sets `white-space:nowrap` for a good reason — a wrapped "Rs 1,23,456" reads as two
    numbers — but the merchant/FBA note is a ~150-character SENTENCE injected into the first cell
    of every expanded size row. Held on one line it made the Product column **780px** instead of
    353px and the table 1526px instead of 1193px, pushing Units, Returns and Rating past the right
    edge of the scroll wrapper.

    Reported as "when I click on the parent sku, the child sku isnt showing units", and the units
    were rendered correctly the whole time: measured in the browser, the Units header sat at x=914
    collapsed and x=1283 expanded. Prose wraps; numbers do not.
    """
    source = _template()
    assert ".chan{" in source, "the channel note has no rule of its own"
    rule = source[source.index(".chan{"):]
    rule = rule[:rule.index("}")]
    assert "white-space:normal" in rule, (
        "the channel note inherits nowrap from `tbody td`, which widens the Product column past "
        "the viewport and pushes the Units column off-screen"
    )
    assert "max-width" in rule, (
        "with no max-width the note claims the column on a wide screen, where there is no "
        "scrollbar to make the cost of it visible"
    )
    body = _function(source, "channelHtml")
    assert 'class="dim chan"' in body, "channelHtml does not use the wrapping class"


def test_every_size_row_still_renders_its_units():
    """The column the report was about. Asserted on the shared builder, so both grains inherit it.

    Kept even though the cause turned out to be layout: had the markup ever been the problem, this
    is the assertion that would have caught it, and it costs nothing to hold both ends.

    The cells moved into `detailCells` when Units, Returns and Rating became optional columns:
    `sizeRowHtml` and the flavour-group row held identical copies of the same seven cells, which is
    two things to keep in step with the toggle. Asserted there now, which covers BOTH grains rather
    than only the flat one.
    """
    body = _function(_template(), "detailCells")
    assert "row.units" in body, "a detail row does not render its units at all"
    # ...and the row builders must go through it rather than keeping a copy.
    assert "detailCells(s)" in _function(_template(), "sizeRowHtml")


# ─── The two nesting levels ──────────────────────────────────────────────────


def test_a_size_row_is_built_by_one_function_for_both_nesting_levels():
    """Flat and flavour-nested size rows must not be two copies of eleven columns.

    Two copies is how a nested row comes to disagree with a flat one about a column — the same
    reason `applySort` is shared by the mouse and the keyboard paths.
    """
    source = _template()
    assert "function sizeRowHtml(" in source, "size rows are built inline rather than once"
    assert "sizeRowHtml(s, true)" in source and "sizeRowHtml(s, false)" in source, (
        "both nesting levels do not go through the shared builder"
    )


def test_the_flavour_grouping_is_rendered_only_when_there_is_more_than_one():
    """85 of the 90 parents have one flavour and must keep a FLAT list of weights.

    A heading level on every product would repeat the parent's own name 85 times to fix 5.
    """
    source = _template()
    assert "p.flavour_groups" in source, "the template ignores the flavour dimension"
    assert "(p.flavour_groups || []).length" in source, (
        "the grouping is not conditional, so single-flavour products grow a pointless heading"
    )
    assert "tr.flav td{" in source, "flavour headings have no style, so the nesting is invisible"
    assert "tr.size.nested td:first-child" in source, (
        "nested size rows are not indented further than their flavour heading"
    )


def test_the_flavour_count_is_on_the_collapsed_parent_row():
    """It is the reason to expand a product, not a fact discovered after doing so.

    15 rows appearing under a name mentioning none of them is what made this unreadable before.
    """
    source = _template()
    assert "(p.flavours || []).length" in source, "the parent row does not count its flavours"
    assert "flavours\n            &times;" in source or "flavours" in source


def test_the_search_still_finds_a_flavour_after_the_parent_was_renamed():
    """**A parent is now named for what its flavours SHARE, so "Cheese" left the parent row.**

    Searching for it would find nothing unless the child names are in the haystack — and
    "Cheese & Cream" is exactly the words the owner used to describe the product.
    """
    source = _template()
    body = source[source.index("const hay = ["):]
    body = body[:body.index(";")]
    assert "s.product" in body, "the size rows' own flavour names are not searchable"
    assert "r.flavours" in body, "the parent's flavour list is not searchable"


# ─── The date picker, for the third time in this codebase ────────────────────


def test_the_window_picker_builds_dates_locally_and_never_through_utc():
    """**`toISOString()` is a UTC formatter, and IST is UTC+5:30.**

    A local `Date` formatted as UTC comes out a day early for the 5½ hours after midnight, so
    between 00:00 and 05:30 IST the picker capped at the day before yesterday and the presets asked
    for the wrong range — refusing a window whose data was already in the database. It only
    misbehaves overnight, which is when nobody is looking.

    CLAUDE.md records the same defect twice already: on the Ads tab (hence the shared `localDate`
    name) and on the Orders tab, where `new Date("2026-08-25")` rendered as 05:30 the following
    morning. Asserted as the ABSENCE of the call across the whole script, because a correct-looking
    fix in one function would not stop it being reintroduced in another.
    """
    source = _template()
    script = source[source.index("<script>"):]
    # Comments are stripped first, because the fix is DOCUMENTED by naming the call it replaced —
    # and an assertion that cannot coexist with its own explanation would force the explanation
    # out. Only executable code is searched, which is the claim being made anyway.
    code = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "toISOString" not in code, (
        "toISOString formats a local date as UTC, which shifts it a day early for 5.5 hours out "
        "of every 24 in IST"
    )
    assert "function localDate(" in source
    body = _function(source, "localDate")
    for getter in ("getFullYear()", "getMonth()", "getDate()"):
        assert getter in body, f"localDate does not use {getter}, so it is not building from local"


def test_the_rating_count_is_deduplicated_per_family():
    """**Amazon pools reviews per variation family, so summing them down the SKU grain triple-counts.**

    Measured on production after the totals row shipped: the SKU view claimed **16,789 reviews where
    4,382 exist** — a 3.8x overstatement printed beside the star as though it were evidence. Every
    size of one product reports the identical rating and count (the 261 rated ASINs carry exactly 90
    distinct pairs), so 267 size rows count the same reviews once per size.

    The average itself barely moves — 3.955 against 3.954 — which is precisely what makes this the
    kind of error that ships: the visible number looks right and the count beside it does not.
    Keying on `parent_asin` counts each family once at either grain.
    """
    body = _function(_template(), "totalsRow")
    assert "r.parent_asin || r.asin" in body, (
        "the rating count is not keyed per family, so the SKU grain counts the same pooled reviews "
        "once per pack size"
    )
    assert "seen.has(family)" in body, "families are not deduplicated before the review count"
    # And the sum still runs over the deduplicated list, not the raw one.
    assert "rated.reduce((a, r) => a + n(r.rating_count), 0)" in body


# ─── The rules panel, grouped by the decision each threshold serves ──────────


def test_the_rules_panel_reads_its_GROUPING_from_the_server():
    """Asked for as "give me editabel metrics for what to put in kill, maintain and scale".

    The grouping is `logic.THRESHOLD_GROUPS`, shipped as `threshold_groups`. A list of group names
    written into the template would be a second copy of a mapping the server already owns — the
    defect this codebase has shipped four times ("86 orders beside 87 lines", `insideDailyCoverage`,
    the verdict groups this panel sits beside) — and it would fail SILENTLY here: a threshold added
    on the server but missing from the template's list still renders, under whichever heading comes
    last, which nobody notices without already knowing where it belonged.
    """
    body = _function(_template(), "renderRules")
    assert "data.threshold_groups" in body, (
        "the panel does not read the server's threshold grouping"
    )
    assert "data.group_order" in body, "the panel does not read the group order from the server"
    for name in ("Scale", "Maintain", "Kill or monitor"):
        assert f'"{name}"' not in body and f"'{name}'" not in body, (
            f"the panel hardcodes the group name {name!r} instead of reading group_order"
        )


def test_an_unmapped_threshold_is_still_EDITABLE_rather_than_vanishing():
    """Deny into the VISIBLE bucket, the rule `verdict_group` already follows.

    A test asserts the mapping is total, so this branch should never fire. It exists because the
    failure it guards is worse than the mess it makes: a threshold with no group would otherwise be
    filtered out of every section and become an un-editable saved setting that still changes every
    verdict — invisible state with no way to reach it, which is the `available`-column defect.
    """
    body = _function(_template(), "renderRules")
    assert "unmapped" in body, "an unmapped threshold has nowhere to render"
    assert "!groups[k]" in body, "the unmapped set is not derived from the server's mapping"


def test_the_retired_acos_threshold_has_no_input_left_behind():
    """`break_even_acos` went with the AD DEPENDENT rule it was the only threshold for.

    An input for a threshold the server no longer accepts would POST a key `save_settings` refuses,
    so the whole save fails with an error naming a number the owner cannot see the purpose of.
    """
    assert "break_even_acos" not in _template()


# ─── Units and Weight are always visible ─────────────────────────────────────


def test_units_and_weight_are_OUTSIDE_the_showExtra_gate_in_ALL_THREE_functions():
    """Asked for as "also add number of units sold" and a total-weight column.

    Units existed but was `extra: true`, so the figure the owner judges a row by was invisible until
    he found the "+ More columns" toggle. Both are now permanent — and that has to be true in all
    three render functions at once, because a cell that is always rendered in the body while the
    header still gates it shifts every column after it.

    Scoped per FUNCTION rather than searched across the file: "this rule holds somewhere in 1,600
    lines" is a different claim from "this function follows it", and the 4th instance of this trap in
    this codebase was a test that passed while one of three call sites disagreed.
    """
    source = _template()
    for name in ("dataCells", "detailCells", "totalsRow"):
        body = _function(source, name)
        # `${showExtra ? ` — the template-literal gate that wraps the optional CELLS. Deliberately
        # not a bare `showExtra ?`: `detailCells` also computes `const trailing = showExtra ? 2 : 1`,
        # which is the colspan rather than a cell, and splitting on that would test the wrong block.
        assert "${showExtra ? `" in body, f"{name} no longer gates the optional columns at all"
        gated = body.split("${showExtra ? `", 1)[1].split('` : ""', 1)[0]
        always = body.replace(gated, "")
        # Asserted on the IDENTIFIERS rather than on the words. The gated block legitimately contains
        # the string "weighted by reviews" — the rating note — so a `"weight" in gated` check fails
        # on prose that has nothing to do with the column. Sixth instance of that substring trap in
        # this codebase, and the first to bite a test I was writing to catch it.
        for cell in ("units", "kg("):
            assert cell not in gated, (
                f"{name} renders {cell!r} inside the showExtra gate, so the column is invisible by "
                "default again"
            )
            assert cell in always, (
                f"{name} renders no {cell!r} cell at all, so the assertion above passes vacuously"
            )


def test_the_weight_total_does_not_COERCE_an_unknown_weight_to_zero():
    """The shared `sum` helper does `n(r[key])`, and `n(null)` is 0 — right for money, wrong here.

    A row whose pack weight the sheet does not carry has an UNKNOWN weight sold. Folded in as 0 kg it
    is the silent shortfall `shipment_weight` names: "a 130 kg shipment reports 90". So weight needs
    its own accumulator, and the excluded rows must be counted and named in the cell.
    """
    body = _function(_template(), "totalsRow")
    assert "weighed" in body, "the weight total has no accumulator of its own"
    assert "r.weight_kg !== null" in body, (
        "the weight total does not filter unknown weights, so nulls are summed as 0 kg"
    )
    assert "weightUnknown" in body, "the excluded rows are not counted"
    # And the count is actually rendered, not merely computed — a working calculation nothing
    # consumes is the defect this codebase has shipped five times.
    assert "no pack weight" in body, "the excluded rows are counted but never named on screen"


def test_the_kg_formatter_shows_a_DASH_and_never_zero_kg():
    """`null` reaches the browser precisely so this distinction survives to the screen."""
    body = _function(_template(), "kg")
    assert "null" in body and "undefined" in body, "kg() does not test for a missing value"
    assert "—" in body, "kg() has no dash for an unknown weight"


# ─── The size rows are plain; the SKU detail row keeps the channel split ──────


def test_a_SIZE_row_carries_no_channel_note_while_the_SKU_row_STILL_DOES():
    """Reported as *"too much info on the left. keep it simple like the parent sku."*

    Both directions, because each failure is real and they are opposite:

    * the note back in `sizeRowHtml` is the reported clutter — a ~150-character sentence in the first
      cell of a numeric row, which is also what widened the Product column 353px -> 780px and pushed
      Units off the scroll wrapper;
    * the note gone from the SKU detail row as well would be "finishing the job", and would delete
      the only remaining view of a split that is decision-relevant: measured, one product's merchant
      SKU spent Rs 1,444 on ads for ZERO attributed sales while its FBA twin returned 36% ACOS.

    Asserted per FUNCTION rather than by counting call sites across the file — an earlier test in this
    codebase counted them, passed at 3, and one of the three was a duplicate.
    """
    source = _template()
    assert "${channelHtml(" not in _function(source, "sizeRowHtml"), (
        "the merchant/FBA sentence is back in the size row's first cell"
    )
    # **Counted as an INTERPOLATION, `${channelHtml(`, not as the bare name.** The comment above
    # `sizeRowHtml` explains the removal and necessarily quotes `channelHtml(s)`, so a bare-name count
    # reads 3 where 2 are rendered. That is the deploy-detector mistake — a substring that also
    # appears in its own explanation — and it bit twice while writing these tests.
    rendered = source.count("${channelHtml(")
    assert rendered == 1, (
        f"{rendered} rows render the channel note; exactly one should (the SKU detail row). "
        "Zero means the split was deleted entirely; two means it is back in a numeric row."
    )
    assert "function channelHtml(" in source, "channelHtml was deleted with its duplicate caller"


def test_the_channel_note_CSS_and_its_wrapping_rule_survive_the_simplification():
    """One caller remains, so all three parts of the original fix still apply.

    Deleting `.chan` along with the duplicate call would leave the SKU detail row's prose inheriting
    `tbody td { white-space: nowrap }` — reintroducing the exact overflow that made child rows look
    as though they had no units.
    """
    source = _template()
    assert ".chan{" in source, "the wrapping rule was deleted, so the surviving note cannot wrap"
    assert "white-space:normal" in source
    assert 'class="dim chan"' in source, "the surviving note no longer uses the wrapping class"


def test_the_screen_says_WHERE_the_channel_split_went():
    """Information that moves without a signpost is information lost.

    The split left the Products view entirely, so the SKUs control names it. Without this the owner
    has no way to discover that the figures he was reading are one click away rather than gone.
    """
    source = _template()
    toggle = source[source.index('data-view="skus"'):]
    toggle = toggle[: toggle.index("</button>")]
    assert "FBA" in toggle, "the SKUs control does not say it holds the Easy Ship / FBA split"
