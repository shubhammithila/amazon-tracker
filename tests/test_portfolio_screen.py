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
    """Eleven columns, so eleven cells — a short row silently shifts every figure left.

    Counted rather than eyeballed: a totals row misaligned by one column would put ad spend under
    TACOS and still look like a plausible table.
    """
    body = _function(_template(), "totalsRow")
    cells = body.count("<td")
    assert cells == 11, f"the totals row has {cells} cells for 11 columns"


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
    """
    body = _function(_template(), "sizeRowHtml")
    assert "n(s.units)" in body, "a size row does not render its units at all"


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
